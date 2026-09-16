# Provenance

Cartograph is a hard fork of two upstream projects. This file records what was
taken, from where, and what changed — satisfying Apache License 2.0 §4(b)
("state significant changes") and keeping cherry-picking of upstream fixes
possible.

## Upstream pins

| Path | Upstream | Licence | Pinned at | Date |
|---|---|---|---|---|
| `engine/` | [tirth8205/code-review-graph](https://github.com/tirth8205/code-review-graph) | MIT | `b58668751ab0c7670c078cf7cbd4d1f5b8e54f81` | 2026-08-26 |

**`memory/` is NOT a fork.** This table listed claude-mem v13.24.23 as a pinned
upstream before anything had been taken from it, and nothing ever was: the
directory has only a LICENSE and a NOTICE, and no claude-mem code is present in
this repository.

That is now the settled position rather than an omission. The memory capability
is being built in Python inside `engine/`, because almost nothing of the
upstream implementation survives the architecture: MCP is removed, Chroma is
replaced by `sqlite-vec`, the tree-sitter grammars are replaced by the engine's
precompiled parser, and summarisation shells out to the host agent. What is left
is a command surface, which is specified independently in the design record
(ticket T10) and verified against this CLI, not against theirs.

So claude-mem is a **design reference**, cited here for honesty about where the
idea came from — not a dependency and not a source of vendored code. If code is
ever taken from it, it gets a row in the table above and a NOTICE entry at that
point, and not before.

Read `thedotmack/claude-mem` upstream if you want the original; the plugin
release distributed through the marketplace is a built artifact (no TypeScript
sources, no licence file) and is not a substitute for the repository.

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
- Replaced the seven bundled MCP-oriented skills with a five-skill pack that
  drives the CLI, shipped as package data and written verbatim by `install`.
- `install` no longer registers an MCP server by default (`--with-mcp` opts
  in), and no longer writes instruction files describing MCP tools.
- Added `carto hook`, moving host hook logic out of per-host shell strings.

Planned for the memory capability (in `engine/cartograph/mem/`, not a fork):

- A CLI query surface under `carto mem`, on the same envelope as everything
  else, replacing an MCP `mcp-search` server.
- `sqlite-vec` instead of Chroma, which upstream reached over MCP via a
  `uvx`-spawned `chroma-mcp` subprocess — removing an MCP dependency, a hidden
  Python/uv dependency and an egress dependency in one change.
- The engine's precompiled parser instead of compiling tree-sitter grammars
  from C at runtime, which required a toolchain no locked-down machine has.

## Fixes offered back upstream

These are genuine upstream defects found while porting, independent of
Cartograph's own direction, so each can be offered as a PR on its own merits.

**How to extract one.** They are NOT isolated commits — that was claimed here
before it was true, and the rename would have stranded them. What exists is the
`pre-rename` tag and the `upstream-fixes` branch, both at `ca83b92`: the last
commit whose module paths still match upstream's layout, so a diff against
`tirth8205/code-review-graph@b586687` is reviewable. After
`84f6d4a` (`code_review_graph` -> `cartograph`) a direct diff is not.

| # | Defect | Upstream | Location | Status |
|---|---|---|---|---|
| 1 | `apply_refactor` defaults to `dry_run=False` — writes by default | code-review-graph | `main.py:794` | not yet submitted |
| 2 | `max_results` mismatch: wrapper says 100, function says 50 | code-review-graph | `main.py:299` vs `tools/review.py:107` | not yet submitted |
| 3 | Lossy knowledge-gap totals — caps applied before truncation | code-review-graph | `analysis.py:206-209` | not yet submitted |
| 4 | `repos` bypasses its own response envelope | code-review-graph | `cli.py:1593-1602` | not yet submitted |
| 5 | `build` hook uses `&`, which does not detach on Windows | code-review-graph | `hooks/hooks.json` | **fixed locally** — `hook.spawn_detached` uses `DETACHED_PROCESS\|CREATE_NEW_PROCESS_GROUP` on Windows and `start_new_session` on POSIX; not yet submitted |
| 6 | `query` CLI exposes only 8 of 16 supported patterns | code-review-graph | `cli.py:1132-1140` | not yet submitted |
| 7 | `tree_sitter_language_pack` imported at module scope for a single call site, putting ~351MB of grammars on the import path of every command — including `status`, which only reads SQLite | code-review-graph | `custom_languages.py:35` | **fixed locally**, not yet submitted |
| 8 | Missing graph exits 1 (usage) when it is a precondition failure, and emits no machine-readable remediation | code-review-graph | `cli.py:1734-1742` | **fixed locally** (Cartograph-specific in part) |
| 9 | A bad grammar name in `languages.toml` crashes the parser instead of warn-and-skip. `tree_sitter_language_pack` resolves grammars through a download manifest and raises `DownloadError`, whose MRO is `(DownloadError, Error, Exception)` — so it is caught by none of `LookupError, ValueError, ImportError, OSError`. One typo takes down every parse. | code-review-graph | `custom_languages.py:342` | **fixed locally** (`3fd38dc`); clean PR candidate, unrelated to Cartograph's direction |

## Design record

The reasoning behind every decision above lives in the Obsidian vault at
`20 Projects/FRQ/MCPs/` — the wayfinder map, 17 resolved decision tickets, the
capability-contract ADR with its amendments, and the reconciliation that settles
conflicts between them.
