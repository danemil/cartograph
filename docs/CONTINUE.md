---
tags: [handoff, cartograph]
updated: 2026-09-16
next-task: rename pass, then carto-hook
---

# CONTINUE HERE

Resumption point for Cartograph. Read this first, verify state with the command
below, then pick up **Next task**.

## Verify state in one command

```bash
cd /Users/emidan/work/cartograph
./scripts/verify.sh
# expect: 139 envelope checks, 224 skills checks, 15/15 copies, "all green"
```

If that passes, everything described here is true. If it does not, trust the
suite over this document.

## What Cartograph is

Two MCP-dependent agent tools re-engineered into one MCP-free toolset, because
MCP servers are banned in the target organisation. Capability is exposed as a
**CLI + skills + hooks** instead. Targets Claude Code, GitHub Copilot CLI and
Copilot Chat. Acceptance test: a fresh machine with only VS Code (Copilot ships
built into VS Code 1.135.0), installing from a private repo, default-deny egress.

- Code: `/Users/emidan/work/cartograph` (private monorepo, Apache-2.0)
- Design record: `/Users/emidan/work/NOTES/20 Projects/FRQ/MCPs/` — wayfinder
  map, 17 resolved tickets, the capability-contract ADR + 9 amendments, and a
  reconciliation that wins where parallel resolutions disagree.

## Done so far (13 code commits)

| Commit | What |
|---|---|
| `e6287ed` | Scaffold: LICENSE, NOTICE, PROVENANCE, envelope schema, skeleton |
| `6d3ed84` | Vendored engine; `carto status`; the envelope; conformance suite |
| `e7f00cd` | All 16 query patterns (was 8); envelope on 10 graph commands |
| `64a9431` | `carto capabilities` — catalogue generated from the argparse parser |
| `478d3b8` | `carto review-context` + `carto review-summary` |
| `9c4c3e8` | `--max-tokens` enforced with semantic truncation |
| `38d5133` | Budget conformance cases pinned to explicit files |
| `e837aa0` | The five-skill pack, verified against the CLI |
| `84f6d4a` | Rename `code_review_graph` -> `cartograph` |
| `31bea8d` | `install` stops shipping MCP; the pack is what gets installed |
| `2eb682b` | Absolute paths no longer leak into review-context |
| `3fd38dc` | Triage of the 56 inherited failures + the real bug behind them |
| `82feb4b` | `carto hook` + cross-platform detached launcher |

The contract is honoured, the capability is reachable by an agent, and it now
fires on its own.

## NEXT TASK — cursors

`next_cursor` is honestly `null` and `has_more` is inferred. A cursor must be
bound to the **query hash plus the provenance snapshot**, so page 2 cannot
silently continue against a graph that was rebuilt in between — that is the
whole reason the contract made cursors opaque and versioned.

Two things to account for:
- `fit()` synthesises a `page` block when a budget forces a trim, so a cursor
  may describe a page the caller never asked for.
- Paging was wrong until recently (see the commit fixing `page.limit`), so do
  not trust any older reasoning about `has_more`.

## Test suite: 1 known failure, not 56

`test_custom_languages.py::TestParserIntegration::test_e2e_nodes_and_edges`.
The erlang grammar is fetched at runtime and its current version nests a remote
call differently, so the assertion encodes an older grammar shape. Nothing in
this fork touches that path. Deliberately NOT skipped — the manifest is
reachable, so a network guard would mislabel a real signal.

```bash
cd engine && .venv/bin/python -m pytest tests/ -q --timeout=300 \
  --ignore=tests/test_agent_transparency.py --ignore=tests/test_embedding_initialization.py \
  --ignore=tests/test_http_origin_guard.py --ignore=tests/test_integration_v2.py \
  --ignore=tests/test_main.py --ignore=tests/test_prompts.py --ignore=tests/test_token_budget.py
```

The 7 ignored modules import `fastmcp`, which is deliberately absent.

## Hooks

`carto hook <event>` (`engine/cartograph/hook.py`) is what hosts call; agents
never do (it is in `_NOT_AGENT_FACING`). Events are named for the job —
`session-status`, `file-update` — with nine host spellings aliased onto them.

**The hook protocol is not the query protocol.** In a hook, exit `2` means
*blocking feedback to the model*, not "precondition failed". `hook.py` never
touches the envelope helpers and always exits 0; a test walks its AST to keep
it that way. `spawn_detached` is the only detach path — `&` does not background
on Windows, and Copilot has no async hook type (30s timeout) against a build
that takes minutes. The Windows branch is asserted at the flag level only;
nobody has run it on Windows.

## The skills pack, and how it stays true

Five skills in `skills/<name>/SKILL.md`, copied into `.claude/skills/`,
`.github/skills/` and `.agents/skills/` by `scripts/install-skills.py`.
Copies, not symlinks: git on Windows checks a symlink out as a text file
containing its target path, which a host reads as a skill body and ignores.

It is also synced into `engine/cartograph/skills_data/`, the package data the
engine ships so `carto install` can write the pack on a machine that never
cloned this repo.

**Never hand-edit a copy** — edit `skills/`, then re-run the installer.
`--check` catches stale, missing and orphaned copies and runs in `verify.sh`.

`carto install` writes the pack to `.claude/skills/`, `.github/skills/` and
`.agents/skills/` (plus Gemini and CodeBuddy), byte-identical to canonical,
and **registers no MCP server** unless `--with-mcp` is passed.

`contracts/capability-v1/check_skills.py` extracts every `carto` line from
every skill body and validates it against `carto capabilities`. This is not
decoration: the designed skill drafts named five commands and flags that do
not exist, and every one was caught this way rather than by review. If you add
a skill, the checker holds it to the same standard automatically.

## Gotchas that cost time already

- **Run from the repo root**, not `engine/`. The conformance manifest uses
  `cwd: engine`; running from inside `engine/` makes it look for `engine/engine`.
- **`PYTHONPATH=engine`** is needed when invoking `python -m cartograph`
  from the repo root (the package is not pip-installed). The venv is at
  `engine/.venv`.
- **Repo-root divergence is real.** `engine/` has no `.git`, so `find_project_root`
  walks up to `cartograph/`. Build and query with the *same* `--repo` or you will
  read an empty graph and conclude the code is broken. This cost two
  misdiagnoses.
- **Never rename the `.code-review-graph` literals.** They name state written
  *before* the rename, so recognising the old spelling is their entire purpose.
  They are `LEGACY_DATA_DIR` / `LEGACY_DB_FILE` in `incremental.py`, with a
  comment saying so. A blanket sed renamed them once and silently disabled the
  legacy migration and half of `uninstall`.
- **Use an ABSOLUTE `PYTHONPATH` when testing hooks.** `spawn_detached` runs
  the child with `cwd` set to the target repo, so a relative `PYTHONPATH=engine`
  resolves against that repo, the import fails, and the failure is invisible
  behind `DEVNULL`. This looks exactly like "detaching does not work".
- **Token budget semantics** are in `docs/design/token-budget.md`. Two traps
  it records: the size block is part of what it measures (so `_with_size`
  iterates to a fixed point), and truncation flags cost characters too (so they
  are installed *before* fitting, not after).
- The engine graph for this repo is built and current (~270 files).
  Rebuild: `PYTHONPATH=engine engine/.venv/bin/python -m cartograph build --repo .`

## After that, in rough priority

1. **Codex / Cursor / OpenCode hooks** still carry raw command lines rather
   than `carto hook`; six upstream assertions pin those exact strings. None is
   a target host, so it was traded away deliberately.
2. **The memory side** (`memory/`) has not been started. Engine-first was the
   decided build order.
3. **The `.vsix`** — the primary delivery vehicle, and the acceptance test:
   a fresh machine with only VS Code.

## Decided, do not relitigate

- Storage is SQLite + `sqlite-vec`. FalkorDB rejected (SSPLv1, needs a daemon);
  Chroma rejected (`chromadb` Node client has no embedded mode).
- Semantic search ships in the **base** install via ONNX (~150–300MB/platform
  after pruning; `onnxruntime-node` is 287MB unpruned).
- Summarisation shells out to the host agent (`copilot -p`), **budgeted and
  visible**: recursion guard, per-session cap, `summary_source` on every memory.
- Build order is engine-first. claude-mem compiled tree-sitter grammars from C
  at runtime, needing a toolchain no locked-down machine has — so it will call
  the engine's precompiled parser instead.
- The `.vsix` is the primary delivery vehicle; installing it also serves Claude
  Code and Copilot CLI.
