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
| **Nothing preinstalled, default-deny egress** | Runtimes, package managers, grammar and model downloads | One `.vsix` per platform carries the frozen engine, all grammars, and the embedding model memory search runs on the CPU; nothing is fetched |
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
and that turn's final reply — or, where `copilot` is not on PATH, by Copilot's
Auto model through VS Code's language-model API, after one consent prompt — and
separates what was **decided** from what was only **proposed**; nothing is pushed into new sessions, and `carto mem status`
reports what memory cost against what it replaced. `carto mem search` finds a
memory by meaning as well as by words (`search_mode: hybrid`): a small
embedding model ships in the `.vsix` and runs locally, with no network and no
Copilot quota. Why this differs from
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

On **Windows**, meaning-based memory search needs Microsoft's Visual C++
runtime (ONNX Runtime depends on `MSVCP140.dll`). Most machines already have
it; a bare one needs the *Microsoft Visual C++ Redistributable (x64)*. Without
it, memory search still works, by keyword, and says why.

In a remote window, run the install from a VS Code terminal *of that window* —
the `code` command there installs on the remote. Cartograph Local is installed
from your local machine (**Extensions: Install from VSIX…**), or accepted when
the extension offers it.

```
code --install-extension carto-linux-x64-<version>.vsix
```

### What it writes into a repository

`carto install` (run by the extension in each workspace) writes the skills
pack to `.github/skills/<skill>/`, the hooks to `.github/hooks/cartograph.json`
and, when run without `--no-instructions`, `.github/instructions/cartograph.instructions.md`;
the graph and memory live in `.cartograph/`. It lists exactly those paths in a
marked block (`# cartograph (managed)` … `# end cartograph`) in the
repository's own `info/exclude`, so `git status` stays clean and nothing is
committed by accident. That file is local and never committed; the tracked
`.gitignore` is not touched. A path the repository already tracks is left out
of the block and named in install's output — exclusion has no effect on a
tracked file. Outside a git repository the step is skipped with a note.
`carto uninstall` removes the block and nothing else in that file.

Releases before 0.6.0 also wrote the pack to `.claude/skills/` and
`.agents/skills/`, which Copilot reads too. `carto install` and `carto
uninstall` remove such a copy only when it is provably Cartograph's: one of the
six skill names, a directory holding only `SKILL.md`, and that file's text a
version the pack has shipped (`engine/cartograph/skills_shipped.json`, built
from git history; line endings aside). A skill of the same name with any other
content is kept and named in the output. `.claude/skills` or `.agents/skills`
is removed only when this emptied it.

The same releases, run with `--platform claude`, merged Claude Code hooks into
`.claude/settings.json`. From 0.9.3, `carto install` and `carto uninstall`
remove a hook there only when its command is identical to one a release wrote
(`engine/cartograph/legacy_hooks.json`); your own hooks and settings stay, and
the file is deleted only when Cartograph's hooks were all it held.
`carto install --dry-run` shows what would go.

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

- `--format json|text` on every command an agent runs; `text` for humans,
  `json` for agents. The exceptions print for a person: `visualize` (its
  `--format` picks an export format), `wiki`, `forget` and `repos`.
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
  semantic ones; a keyword answer from `mem search` carries
  `semantic_unavailable`, the reason.

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

Not yet: summaries that read the agent's replies rather than only the prompts. Memory search is hybrid
(keyword + a bundled all-MiniLM-L6-v2) wherever the payload's model is found,
and says `keyword`, with the reason, where it is not; the graph's own `search`
uses the same model only after `carto embed` has been run.

Release `.vsix` files are built by CI for linux-x64, win32-x64 and
darwin-arm64, and the Linux build is smoke-tested offline on Ubuntu 20.04,
Ubuntu 22.04 and Debian 11. Linux over Remote SSH has been run by hand
end to end; Windows and Dev Containers have been tested in CI only. See
`docs/packaging.md`. What changed in each release: [CHANGELOG.md](CHANGELOG.md).

To check memory on a real machine, step by step: [docs/verify-memory.md](docs/verify-memory.md).

Verify any of this rather than trusting it:

```bash
./scripts/verify.sh     # envelope conformance + skills-match-CLI + install sync
```

`docs/CONTINUE.md` is the resumption point and says what is next.

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
