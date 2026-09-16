# Provenance

Cartograph is a hard fork of two upstream projects. This file records what was
taken, from where, and what changed — satisfying Apache License 2.0 §4(b)
("state significant changes") and keeping cherry-picking of upstream fixes
possible.

## Upstream pins

| Path | Upstream | Licence | Pinned at | Date |
|---|---|---|---|---|
| `engine/` | [tirth8205/code-review-graph](https://github.com/tirth8205/code-review-graph) | MIT | `b58668751ab0c7670c078cf7cbd4d1f5b8e54f81` | 2026-08-26 |
| `memory/` | [thedotmack/claude-mem](https://github.com/thedotmack/claude-mem) | Apache-2.0 | release **v13.24.23** | snapshot 2026-09-15 |

**Note on `memory/`:** taken from a release tarball with no git metadata, so it
is pinned by released version rather than commit SHA. Upstream `main` had moved
to `dcfc44221deabc228b0698a0012f87ed2fe6bbe7` by 2026-09-16. Re-clone with
history if an exact SHA becomes necessary.

## Significant changes

Applied to both forks:

- **Removed all MCP transports.** MCP servers are prohibited in the target
  environment. Capability is exposed through a CLI, skills and hooks instead.
- **Renamed** to Cartograph / `carto`.
- **Adopted a shared capability contract** — a versioned JSON envelope, an
  exit-code scheme, mandatory paging, and a conformance suite.

Specific to `engine/` (ex code-review-graph):

- Dropped `mcp` / `fastmcp` dependencies, and with them the starlette/uvicorn
  HTTP stack.
- Closed the CLI/MCP parity gap: 10 new commands, ~12 commands gained missing
  flags, ~25 gained the contract envelope.
- Became the single tree-sitter parser for the whole project.

Specific to `memory/` (ex claude-mem):

- Replaced the `mcp-search` server with a CLI query surface under `carto mem`.
- Replaced the Chroma vector store — reached over MCP via a `uvx`-spawned
  `chroma-mcp` subprocess — with `sqlite-vec`, removing an MCP dependency, a
  hidden Python/uv dependency and an egress dependency in one change.
- Stopped compiling tree-sitter grammars from C source at runtime; `smart_*`
  now calls `engine/`'s precompiled parser. This removes a runtime C-toolchain
  requirement that could not be satisfied on a locked-down machine.
- Added a `copilot` host adapter.

## Fixes offered back upstream

These are genuine upstream defects found while porting, independent of
Cartograph's own direction. Each is kept as an isolated commit against pristine
upstream so it can be offered as a PR on its own merits.

| # | Defect | Upstream | Location | Status |
|---|---|---|---|---|
| 1 | `apply_refactor` defaults to `dry_run=False` — writes by default | code-review-graph | `main.py:794` | not yet submitted |
| 2 | `max_results` mismatch: wrapper says 100, function says 50 | code-review-graph | `main.py:299` vs `tools/review.py:107` | not yet submitted |
| 3 | Lossy knowledge-gap totals — caps applied before truncation | code-review-graph | `analysis.py:206-209` | not yet submitted |
| 4 | `repos` bypasses its own response envelope | code-review-graph | `cli.py:1593-1602` | not yet submitted |
| 5 | `build` hook uses `&`, which does not detach on Windows | code-review-graph | `hooks/hooks.json` | not yet submitted |
| 6 | `query` CLI exposes only 8 of 16 supported patterns | code-review-graph | `cli.py:1132-1140` | not yet submitted |
| 7 | `tree_sitter_language_pack` imported at module scope for a single call site, putting ~351MB of grammars on the import path of every command — including `status`, which only reads SQLite | code-review-graph | `custom_languages.py:35` | **fixed locally**, not yet submitted |
| 8 | Missing graph exits 1 (usage) when it is a precondition failure, and emits no machine-readable remediation | code-review-graph | `cli.py:1734-1742` | **fixed locally** (Cartograph-specific in part) |

## Design record

The reasoning behind every decision above lives in the Obsidian vault at
`20 Projects/FRQ/MCPs/` — the wayfinder map, 17 resolved decision tickets, the
capability-contract ADR with its amendments, and the reconciliation that settles
conflicts between them.
