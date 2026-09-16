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

## Done so far (8 code commits)

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

The envelope contract is now fully honoured: everything it declares, it does.
And the capability is reachable by an agent, not only at a terminal.

## NEXT TASK — `carto install` destroys the skills pack

Found while renaming, and it defeats the work two commits earlier.

`generate_skills()` (`engine/cartograph/skills.py:995`) writes upstream's
bundled skills into `repo_root/.claude/skills/<name>/SKILL.md`. **All five of
its names collide exactly with ours** — build-graph, debug-issue,
explore-codebase, refactor-safely, review-changes — and upstream's bodies tell
the agent to *"use the cartograph MCP tools"*, which do not exist here. So a
normal `carto install` silently overwrites five verified skills with five that
instruct the agent to call a banned, absent MCP server.

The content lives in three places that must agree:

| Where | What |
|---|---|
| `skills/*/SKILL.md` | ours, canonical, verified by `check_skills.py` |
| `engine/cartograph/skills.py:875` `_SKILLS` | a Python dict of name/description/body — what `install` actually writes |
| `engine/skills/*/SKILL.md` | a bundled mirror; `test_pr779_edges` asserts it is byte-identical to `_SKILLS` |

**Recommended fix — make `skills/` the only source.** Ship the pack as package
data (`engine/cartograph/skills_data/`) and have `generate_skills()` read it
with `importlib.resources` instead of carrying bodies in a Python dict. Then
`install-skills.py` syncs one more destination, `verify.sh` covers it, and the
byte-identical test becomes true by construction rather than by discipline.
Drop `review-delta` and `review-pr` at the same time — T09 collapsed both into
`review-changes`, and they differ only in scope.

Do not simply edit `_SKILLS` to match: three copies kept in step by hand is how
this happened.

### Related, same root cause

- **`_legacy_instructions.py` is live.** `carto install` injects instruction
  files describing MCP tools, gated by `--no-instructions`. Same problem, same
  command, different artifact.
- **`engine/.mcp.json`** still ships an MCP server definition.

## The skills pack, and how it stays true

Five skills in `skills/<name>/SKILL.md`, copied into `.claude/skills/`,
`.github/skills/` and `.agents/skills/` by `scripts/install-skills.py`.
Copies, not symlinks: git on Windows checks a symlink out as a text file
containing its target path, which a host reads as a skill body and ignores.

**Never hand-edit the copies** — edit `skills/`, then re-run the installer.
`--check` catches stale, missing and orphaned copies and runs in `verify.sh`.

`contracts/capability-v1/check_skills.py` extracts every `carto` line from
every skill body and validates it against `carto capabilities`. This is not
decoration: the designed skill drafts named five commands and flags that do
not exist, and every one was caught this way rather than by review. If you add
a skill, the checker holds it to the same standard automatically.

## Contract gaps found while writing the skills

Three commands sit outside the envelope contract. The skills route around them,
so nothing is broken — but they are the remaining inconsistencies in the
agent-facing surface, and they are why `carto capabilities` lists flags that
differ between commands.

- **`detect-changes`** emits raw JSON, not an envelope, and has no `--format`
  or `--max-tokens`. `review-summary` covers the same ground inside the
  contract, which is what the skills use instead.
- **`dead-code`** emits raw JSON behind `--json` rather than `--format json`,
  and **prints `INFO: ...` to stdout before it** — a direct violation of the
  envelope's first rule, that stdout carries nothing but the envelope. An
  agent parsing it gets a JSONDecodeError. `carto refactor dead_code` is the
  enveloped path.
- **`_PAGEABLE_COLLECTION["refactor"]`** is `"matches"`, but `refactor suggest`
  returns `suggestions`. `fit()`'s sole-list fallback handles it, so nothing
  misbehaves; the map is just incomplete.

Retrofitting the first two is a small, well-understood job — the pattern is
`_emit_tool_result`, already used by twelve commands.

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
- **`PYTHONPATH=engine`** is needed when invoking `python -m cartograph`
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
  Rebuild: `PYTHONPATH=engine engine/.venv/bin/python -m cartograph build --repo .`

## After that, in rough priority

1. **The rename pass** — `code_review_graph` → `cartograph` (done). Cheap now, worse
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
