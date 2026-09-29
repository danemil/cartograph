# Cartograph

A code knowledge graph and persistent session memory for AI coding agents —
**with no MCP server**.

Cartograph exposes its capability the way the hosts already work: a **CLI**, a
**skills pack**, and **hooks**. It runs on **GitHub Copilot CLI** and **GitHub
Copilot Chat** in VS Code, and installs on a machine with nothing on it.

```
carto status                      # is there a graph here?
carto query callers_of parse_args # who calls this?
carto review-context --base main  # token-efficient review context
carto capabilities                # everything else, machine-readable
```

## Why

MCP servers are prohibited in the target environment. Both upstream tools
delivered their value through MCP. Cartograph rebuilds that value from
primitives every host supports natively.

It is a hard fork and unification of
[code-review-graph](https://github.com/tirth8205/code-review-graph) (MIT) and
[claude-mem](https://github.com/thedotmack/claude-mem) (Apache-2.0).
See [PROVENANCE.md](PROVENANCE.md).

## The constraints it is built for

Every design decision below traces to one of these. They are the target
environment's rules, not preferences.

| Constraint | What it breaks | How Cartograph meets it |
|---|---|---|
| **No MCP servers** | Both upstream tools delivered their value through MCP | A CLI (`carto`), a skills pack and hooks — what every host already runs |
| **The only AI tool is GitHub Copilot** — Chat and CLI, in VS Code | Anything that assumes Claude Code or another agent | Copilot is the only host: `carto install` writes only what Copilot reads, and session summaries are written by `copilot -p` or a labelled structural fallback — never another agent |
| **Nothing preinstalled, default-deny egress** | Runtimes, package managers, grammar downloads | One `.vsix` per platform carries the frozen engine and all grammars; nothing is fetched |
| **Chat hooks can be disabled by organisation policy** (`chat.useHooks`) | Automatic capture from Chat — the hook never runs, silently | Memory is also imported from the conversation logs that exist regardless ([below](#where-memory-comes-from)); the extension says once when hooks are off |
| **Copilot CLI runs repository hooks only in a trusted folder** | CLI capture in an untrusted folder, silently | Trust the folder once; otherwise the CLI's own session log is imported |
| **Development happens in a VM or container** — Remote SSH, Dev Containers, WSL | VS Code keeps Chat history on the *local* machine, the engine runs on the *remote* | **Cartograph Local**, a small companion extension on the local side, passes Chat history to the remote |
| Extension allow-lists (possible) | Installing either `.vsix` | Needs the organisation's approval — plan for two extensions |

### Where memory comes from

Hooks first; logs whenever hooks did not record something. Session ids match
across both, so nothing is recorded twice, and `carto mem status` reports per
host whether memory is arriving via **hooks** or **logs**.

| | Copilot Chat | Copilot CLI (terminal inside VS Code) |
|---|---|---|
| **Hooks allowed** | Chat hooks, on whichever side Copilot Chat runs | CLI hooks, in a trusted folder |
| **Hooks blocked, local window** | VS Code's `chatSessions/`, read directly | `~/.copilot/session-state/`, read directly |
| **Hooks blocked, remote window** | VS Code's `chatSessions/` on the local machine, copied to the remote by **Cartograph Local** | `~/.copilot/session-state/` on the remote, read directly |

The graph, the skills and session summaries work the same in every row. One
summary per session is written by `copilot -p --model auto` from each prompt
and that turn's final reply, and separates what was **decided** from what was
only **proposed**; nothing is pushed into new sessions, and `carto mem status`
reports what memory cost against what it replaced. Why this differs from
claude-mem: [docs/memory-design.md](docs/memory-design.md).
Details and evidence: [docs/copilot-hooks.md](docs/copilot-hooks.md). Step-by-step
check on a real machine: [docs/verify-memory.md](docs/verify-memory.md).

## Installing

From the release page, install:

- **`carto-<platform>-<version>.vsix`** — the engine, the skills, the hooks.
  Pick the platform the *engine* runs on: in a remote window that is the
  remote (a Linux VM or container → `linux-x64`), not your laptop.
- **`cartograph-local-<version>.vsix`** — only for remote windows. It installs
  on your local machine. The main extension offers to install it when it sees
  a remote window with Chat hooks off.

In a remote window, run the install from a VS Code terminal *of that window* —
the `code` command there installs on the remote. Cartograph Local is installed
from your local machine (**Extensions: Install from VSIX…**), or accepted when
the extension offers it.

```
code --install-extension carto-linux-x64-<version>.vsix
```

## Layout

| Path | What |
|---|---|
| `engine/` | Python — the graph and the tree-sitter parser |
| `engine/cartograph/mem/` | Observations and recall — same binary, same envelope |
| `contracts/capability-v1/` | The capability contract: schemas, fixtures, conformance suite |
| `skills/` | The skills pack — one set, installed to `.github/skills/`, which Copilot CLI and Copilot Chat both read |
| `hooks/` | Host hook manifests (the logic lives in `engine/cartograph/hook.py`) |
| `extension/` | VS Code extension — the primary delivery vehicle |
| `companion/` | Cartograph Local — the local-side companion for remote windows |
| `installer/` | Bootstrap for Copilot CLI on a machine without VS Code |
| `scripts/` | Build and release tooling |

## The contract

Every command speaks one envelope. It is the seam between Cartograph and any
host — there is no per-host adapter on the query path.

```json
{
  "schema": 1,
  "ok": true,
  "tool": "review-context",
  "data": { "items": [], "summary": {} },
  "truncated": false,
  "size": { "chars": 7360, "tokens_estimated": 1840, "estimator": "chars/4" },
  "provenance": { "graph_sha": "…", "built_at": "…" },
  "page": { "limit": 50, "next_cursor": null, "has_more": false, "result_count": 12 }
}
```

- `--format json|text` on every command; `text` for humans, `json` for agents.
- `--max-tokens N` everywhere. Truncation is **semantic** — lowest-ranked
  results are dropped, never a byte cut.
- Exit codes: `0` success (**empty results are success**) · `1` usage ·
  `2` precondition, always with an actionable `error.remediation` ·
  `3` internal.
- In `json` mode **stdout carries nothing but the envelope**. Logs go to stderr.
- Cursors are bound to the query hash *and* the provenance snapshot, so page two
  cannot silently continue against a rebuilt graph. *(Specified; `next_cursor`
  is still `null` — see Status.)*

Schema: [`contracts/capability-v1/schemas/envelope.schema.json`](contracts/capability-v1/schemas/envelope.schema.json).

Two protocols exist and must not be conflated: this **query protocol**, and the
host-defined **hook protocol** where exit `2` means *blocking feedback to the
model*, not "precondition failed".

## Design record

The reasoning lives in the Obsidian vault at `20 Projects/FRQ/MCPs/` — a
wayfinder map, 17 resolved decision tickets, the capability-contract ADR with
nine amendments, and a reconciliation settling conflicts between parallel
resolutions.

Some decisions worth knowing before changing things:

- **No graph database, no Chroma.** FalkorDB is SSPLv1 and needs a daemon;
  `chromadb`'s Node client has no embedded mode. Storage is SQLite +
  `sqlite-vec`, which both languages embed and which keeps everything in one file.
- **Grammars are precompiled, never built at runtime.** The upstream memory tool
  shelled out to the `tree-sitter` CLI to compile grammars from C, requiring a
  toolchain no locked-down machine has. One parser, two consumers.
- **Hooks always detach.** Copilot has no async hook type and a 30s default,
  against a build that takes minutes. Hosts call `carto hook <event>`, which
  hands off through one `spawn_detached` and returns in ~0.2s. A trailing `&`
  is the reflex and it backgrounds nothing on Windows.
- **Degradation is always visible.** `search_mode`, `summary_source`, the
  community algorithm name. An agent must never mistake keyword results for
  semantic ones.

## Status

**The engine's contract is complete, and memory works on the hosts the target
environment allows — Copilot CLI and Copilot Chat.** Every prompt is captured
by a hook, each session is summarised into WORKED ON / DECIDED / DEAD ENDS by
the host's own Copilot CLI (or a structural fallback, labelled as such), and
the graph refreshes at the end of every agent turn. All of it was proven live
against both hosts; see [docs/copilot-hooks.md](docs/copilot-hooks.md).

Memory is Python inside the engine rather than a TypeScript fork. Almost
nothing of the upstream implementation survives this architecture, and a
second runtime would have to be installed on every target machine, carry a
second envelope implementation, and be held to the contract separately. See
PROVENANCE.md.

Working today: the capability envelope on ~25 commands, all 16 query patterns,
`carto capabilities`, `carto review-context` / `review-summary`, `--max-tokens`
with semantic truncation, the six-skill pack, `carto mem add|search|show|status|summarise|sync`,
hooks for Copilot CLI and Copilot Chat, memory from Copilot's own
logs when hooks are blocked, and Cartograph Local for remote windows.

Not yet: cursors (`next_cursor` is honestly `null`), semantic search
(everything reports `search_mode: keyword`), and summaries that read the
agent's replies rather than only the prompts.

The `.vsix` is built and verified **on darwin-arm64 only**. PyInstaller freezes
the interpreter it runs on, so every other target has to be built on its own
machine, and none has been. The extension's activation path has not been run
inside a VS Code window — what was verified is the `.vsix` installing through
`code --install-extension`, and the placement and query code driven directly.
See `docs/packaging.md`.

To check memory on a real machine, step by step: [docs/verify-memory.md](docs/verify-memory.md).

Verify any of this rather than trusting it:

```bash
./scripts/verify.sh     # envelope conformance + skills-match-CLI + install sync
```

`docs/CONTINUE.md` is the resumption point and says what is next.

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
