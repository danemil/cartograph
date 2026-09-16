---
tags: [handoff, cartograph]
updated: 2026-09-16
next-task: enforce --max-tokens
---

# CONTINUE HERE

Resumption point for Cartograph. Read this first, verify state with the command
below, then pick up **Next task**.

## Verify state in one command

```bash
cd /Users/emidan/work/cartograph
engine/.venv/bin/python contracts/capability-v1/check.py --manifest engine/contract-manifest.json
# expect: engine: 75/75 checks passed
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

## Done so far (4 code commits)

| Commit | What |
|---|---|
| `e6287ed` | Scaffold: LICENSE, NOTICE, PROVENANCE, envelope schema, skeleton |
| `6d3ed84` | Vendored engine; `carto status`; the envelope; conformance suite |
| `e7f00cd` | All 16 query patterns (was 8); envelope on 10 graph commands |
| `64a9431` | `carto capabilities` — catalogue generated from the argparse parser |
| `478d3b8` | `carto review-context` + `carto review-summary` |

## NEXT TASK — enforce `--max-tokens`

`--max-tokens` is accepted on every command and **currently ignored**. This is
the last piece of the envelope contract that is declared but not honoured.

It matters most on `review-context`, which runs **~6,263 tokens at `--limit 3`**,
dominated by `facets.source_snippets`.

**Requirements (from the contract, `docs/decisions/2026-09-15-capability-contract.md`):**

1. **Truncation is SEMANTIC, never a byte cut.** Drop lowest-value content, keep
   the envelope valid JSON. Never truncate mid-structure.
2. When truncation happens, set `truncated: true` **and** `truncated_reason`
   (`max_tokens` here, vs `page_limit` which paging already sets).
3. `size.tokens_estimated` must reflect what was actually emitted.
4. The drop order should be deliberate and documented — suggested, least
   valuable first: `facets.source_snippets` → `facets.edges.items` →
   `facets.changed_nodes.items` → trim `data.items` from the tail.
   `data.summary` is never dropped; it is the part an agent always needs.
5. Add conformance cases: a `--max-tokens` small enough to force truncation must
   still produce a schema-valid envelope with `truncated_reason: "max_tokens"`.

**Where to implement:** `engine/code_review_graph/envelope.py` is the natural
home — a `fit(env, max_tokens, drop_order)` helper applied in `emit()`, so every
command inherits it rather than each reimplementing it. `_emit_tool_result` in
`cli.py` already passes through `args.max_tokens`.

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
- The engine graph for this repo is built and current (272 files, 5796 nodes).
  Rebuild: `PYTHONPATH=engine engine/.venv/bin/python -m code_review_graph build --repo .`

## After that, in rough priority

1. **The skills pack** — five skills are designed in the vault
   (`docs/decisions/T09-skills-resolution.md`), with `review-changes` drafted in
   full. This is what makes any of it reachable by an agent rather than only at
   a terminal.
2. **The rename pass** — `code_review_graph` → `cartograph`. Cheap now, worse
   later, and agents act on remediation strings literally.
3. **`carto-hook` + detached launcher** — makes it fire automatically.
   Copilot has no async hook type and `&` does not detach on Windows.
4. **Cursors** — `next_cursor` is honestly `null`; `has_more` is inferred from a
   full page. Must be bound to query hash + provenance so page 2 cannot continue
   against a rebuilt graph.

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
