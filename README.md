# Cartograph

A code knowledge graph and persistent session memory for AI coding agents —
**with no MCP server**.

Cartograph exposes its capability the way the hosts already work: a **CLI**, a
**skills pack**, and **hooks**. It runs on **Claude Code**, **GitHub Copilot
CLI** and **GitHub Copilot Chat** in VS Code, and installs on a machine with
nothing on it.

```
carto status                      # is there a graph here?
carto query callers_of parse_args # who calls this?
carto review-context --base main  # token-efficient review context
carto mem search "auth refactor"  # what did we learn before?
```

## Why

MCP servers are prohibited in the target environment. Both upstream tools
delivered their value through MCP. Cartograph rebuilds that value from
primitives every host supports natively.

It is a hard fork and unification of
[code-review-graph](https://github.com/tirth8205/code-review-graph) (MIT) and
[claude-mem](https://github.com/thedotmack/claude-mem) (Apache-2.0).
See [PROVENANCE.md](PROVENANCE.md).

## Layout

| Path | What |
|---|---|
| `engine/` | Python — the graph and the tree-sitter parser |
| `memory/` | TypeScript — observations and recall |
| `contracts/capability-v1/` | The capability contract: schemas, fixtures, conformance suite |
| `skills/` | The skills pack — one set, read by all three hosts |
| `hooks/` | Hook manifests: Claude Code hand-written, Copilot generated |
| `extension/` | VS Code extension — the primary delivery vehicle |
| `installer/` | Bootstrap for the CLI hosts |
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
  cannot silently continue against a rebuilt graph.

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
- **Hooks always detach.** Copilot has no async hook type and a 30s default, and
  `process.exit()` kills in-flight work. Every capture hook hands off to
  `carto-hook --detach` and exits.
- **Degradation is always visible.** `search_mode`, `summary_source`, the
  community algorithm name. An agent must never mistake keyword results for
  semantic ones.

## Status

Scaffolded. Nothing is implemented yet.

First milestone — the vertical slice, in this order:
`carto status` → `carto query` → `carto review-context`.
Envelope first on the smallest surface, then breadth (the CLI currently reaches
only 8 of 16 query patterns), then the headline capability.

## Licence

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
