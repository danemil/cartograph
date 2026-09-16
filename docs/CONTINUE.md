---
tags: [handoff, cartograph]
updated: 2026-09-16
next-task: the skills pack
---

# CONTINUE HERE

Resumption point for Cartograph. Read this first, verify state with the command
below, then pick up **Next task**.

## Verify state in one command

```bash
cd /Users/emidan/work/cartograph
engine/.venv/bin/python contracts/capability-v1/check.py --manifest engine/contract-manifest.json
# expect: engine: 139/139 checks passed
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

## Done so far (6 code commits)

| Commit | What |
|---|---|
| `e6287ed` | Scaffold: LICENSE, NOTICE, PROVENANCE, envelope schema, skeleton |
| `6d3ed84` | Vendored engine; `carto status`; the envelope; conformance suite |
| `e7f00cd` | All 16 query patterns (was 8); envelope on 10 graph commands |
| `64a9431` | `carto capabilities` — catalogue generated from the argparse parser |
| `478d3b8` | `carto review-context` + `carto review-summary` |
| `9c4c3e8` | `--max-tokens` enforced with semantic truncation |

The envelope contract is now fully honoured: everything it declares, it does.

## NEXT TASK — the skills pack

Five skills are designed in the vault (`docs/decisions/T09-skills-resolution.md`),
with `review-changes` drafted in full. **This is what makes any of the above
reachable by an agent rather than only at a terminal** — until it exists,
Cartograph is a CLI nobody's agent knows to call.

Discovery is shared across all three hosts: `.github/skills/`, `.agents/skills/`,
`.claude/skills/`. Skill bodies stay short and point at `carto capabilities`
for the long tail — that is the whole reason the catalogue exists.

## Known state you should not mistake for a regression

**56 engine tests fail, and did so before this work too.** Verified by running
the suite on the stashed tree: identical `56 failed, 2717 passed` on both
sides. They are upstream tests asserting pre-contract behaviour — e.g.
`assert exc_info.value.code == 1` where the missing-graph guard now exits `2`
(PRECONDITION, deliberately), and `SystemExit: 0` where graph-tool commands now
`raise SystemExit(emit(...))`. Two more need network to fetch a grammar
manifest. They need triage, not panic — but triage them before trusting the
suite, because a real regression could hide among them.

Run them with the MCP-path modules excluded (no `fastmcp` in the venv, by
design — MCP is banned):

```bash
cd engine && .venv/bin/python -m pytest tests/ -q --timeout=300 \
  --ignore=tests/test_agent_transparency.py --ignore=tests/test_embedding_initialization.py \
  --ignore=tests/test_http_origin_guard.py --ignore=tests/test_integration_v2.py \
  --ignore=tests/test_main.py --ignore=tests/test_prompts.py --ignore=tests/test_token_budget.py
```

## Worth fixing when you are next in `review_shape.py`

`_node_to_item` falls back to `node["qualified_name"]`, which for file-kind
nodes is an **absolute path** — so `facets.changed_nodes[].id` and `.title`
carry `/Users/…/cartograph/engine/…`. That is exactly the noise `_relativise`
exists to prevent, in the one place it was not applied. Cheap, and it is
context an agent pays for on every call.

## Gotchas that cost time already

- **Run from the repo root**, not `engine/`. The conformance manifest uses
  `cwd: engine`; running from inside `engine/` makes it look for `engine/engine`.
- **`PYTHONPATH=engine`** is needed when invoking `python -m code_review_graph`
  from the repo root (the package is not pip-installed). The venv is at
  `engine/.venv`.
- **Repo-root divergence is real.** `engine/` has no `.git`, so `find_project_root`
  walks up to `cartograph/`. Build and query with the *same* `--repo` or you will
  read an empty graph and conclude the code is broken. This cost two
  misdiagnoses.
- **`code-review-graph` is still the package name.** The rename pass has not
  happened; several internal messages still tell agents to run
  `code-review-graph build`. Remediation strings already say `carto build`.
- **Token budget semantics** are in `docs/design/token-budget.md`. Two traps
  it records: the size block is part of what it measures (so `_with_size`
  iterates to a fixed point), and truncation flags cost characters too (so they
  are installed *before* fitting, not after).
- The engine graph for this repo is built and current (253 files, 5746 nodes).
  Rebuild: `PYTHONPATH=engine engine/.venv/bin/python -m code_review_graph build --repo .`

## After that, in rough priority

1. **The rename pass** — `code_review_graph` → `cartograph`. Cheap now, worse
   later, and agents act on remediation strings literally.
2. **Triage the 56 failing tests** (see above) — mostly delete-or-update, but
   it has to be done before the suite is a safety net again.
3. **`carto-hook` + detached launcher** — makes it fire automatically.
   Copilot has no async hook type and `&` does not detach on Windows.
4. **Cursors** — `next_cursor` is honestly `null`; `has_more` is inferred from
   a full page. Must be bound to query hash + provenance so page 2 cannot
   continue against a rebuilt graph. Note `fit()` already synthesises a `page`
   block when a budget forces a trim, so cursors must account for a page the
   caller never asked for.

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
