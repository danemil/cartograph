---
tags: [design, contract, token-budget, engine]
created: 2026-09-29
status: implemented
---

# Compact rows — what a list command costs

`--max-tokens` (see `token-budget.md`) decides what to give up when a response
is too large. This is the other half: what a response costs before any budget
applies. The two are independent — compaction runs first, and `fit()` then
works on the rows it produced.

## Why

A user asked GitHub Copilot "which are the largest functions?" on a 70-file
repository. `carto large-functions --min-lines 80 --limit 20` returned 8,362
characters (~2,091 tokens); `find | xargs wc -l | sort` returned 1,300 (~325).
Reproduced on a 991-file TypeScript repository (claude-mem at `ade13f3`), the
response had three defects:

- **Wrong answer.** Of 20 "large functions", 15 were File nodes, 4 Class, 1
  Test — none were functions. The first was a generated 14,726-line `.d.ts`.
- **Row bloat.** Twelve fields per row, ~356 characters. For a File node,
  `name`, `qualified_name`, `file_path` and `relative_path` are the same path;
  add `id`, `parent_name: null`, `is_test: false` and `language`.
- **A second copy.** `data.summary` restated the first ten rows as prose.

## What changed

| | Before | After |
|---|---|---|
| `large-functions` kinds | every kind | `Function` (methods included — the graph has no Method kind; a method is a Function with a parent). `--kind` widens and repeats. |
| Generated files | ranked | excluded and counted (`data.excluded`, and the summary), `--include-generated` to rank them. The test is `incremental.is_generated_file`: the build's own `DEFAULT_IGNORE_PATTERNS` plus `*.d.ts`/`*.d.mts`/`*.d.cts`. |
| Row shape | the whole node dict | one string per row; `--detail full` for the dict |
| Summary | multi-line, or the list restated | one line |

A compact row keeps what an agent acts on and nothing it already has:

```
301 lines | Function | Store.migrate | src/store.ts:20      node, with the ranked number
Function | a.py::f | line 1                                  query disambiguation (a target)
CALLS | runMatrix -> src/store.ts::Store.migrate | scripts/run.ts:512
id 1 | constructor | criticality 0.8906 | 72 nodes, 8 files, depth 6
id 12 | sync-real | 6605 nodes | cohesion 0.1435
```

A node's qualified name — what `carto query` takes as a target — is
`<path>::<name>` from the row. Disambiguation rows carry it whole, because
`query`'s own hint says to pass one back.

Also dropped in compact mode: `_hints` (they name MCP tools that no longer
exist), `status: "ok"`, nulls, `query`'s `candidates` (the same list as
`disambiguation`), echoes of the caller's own arguments, and the copies of
`truncated` and `search_mode` the envelope already carries. The emit path reads
those two from the tool result *before* compacting; dropping them first would
lose `page_limit` truncation and the search-mode degradation signal.

## One place

`engine/cartograph/compact.py` is the only code that shortens a row. Each
command's entry names which of its lists hold which kind of row; nothing else
about a command lives there. The parser attaches `--detail` from the same
`COMMANDS` set, so a command cannot advertise a flag that does nothing for it
(a test asserts both directions). `dead-code --format text` used to format its
own rows; it now prints `compact.node_row`.

## Commands, and the ones left alone

| Command | Compact rows | Why |
|---|---|---|
| large-functions, query, search, impact, dead-code, flows, communities, review-context | yes | measured below |
| architecture | no | not a list; 4,211 chars on claude-mem already |
| refactor | no | its rows are distinct prose fields with no repeated path, and `rename` edits are applied, so a lossy row is a risk; not measured |
| flow, community | no | single-object drill-downs, not lists |
| detect-changes | no | no pageable collection, and no non-empty response to measure on either repo (claude-mem has one commit) |
| mem search | no | rows are observations, already paired with `mem show` for the full text |

## Measured

claude-mem at `ade13f3` (991 files, graph already built), stdout characters of
the default `--format json` invocation, tokens as chars/4 (ceil). "Before" is
`22d3755`, "after" is the commit that adds this document. `--detail full` is
the after code with the old row shape.

| Command | Before chars | Before tok | After chars | After tok | Change | `--detail full` chars |
|---|---:|---:|---:|---:|---:|---:|
| `large-functions --min-lines 80 --limit 20` | 10,897 | 2,725 | 2,577 | 645 | −76% | 10,479 |
| `query callers_of src/services/sqlite/SessionStore.ts::SessionStore.createSDKSession --limit 20` | 19,266 | 4,817 | 7,196 | 1,799 | −63% | 19,266 |
| `search session --limit 20` | 12,844 | 3,211 | 3,022 | 756 | −76% | 12,844 |
| `impact --files src/services/sqlite/SessionStore.ts` | 1,361,508 | 340,377 | 506,392 | 126,598 | −63% | 1,361,443 |
| `dead-code --limit 20 --format json` | 8,192 | 2,048 | 1,918 | 480 | −77% | 8,192 |
| `flows --limit 20` | 19,465 | 4,867 | 2,223 | 556 | −89% | 19,465 |
| `communities` | 13,202 | 3,301 | 1,157 | 290 | −91% | 13,202 |
| `review-context --files src/services/sqlite/SessionStore.ts` | 45,476 | 11,369 | 19,048 | 4,762 | −58% | 44,766 |

`large-functions` after returns 20 functions and methods; before it returned
no functions at all. Its `--detail full` column is the same rows whole, which
is why it is not the "before" number. For comparison, the file-level question
answered with the shell — `git ls-files '*.ts' '*.tsx' '*.js' '*.py' | xargs
wc -l | sort -n | tail -21 | head -20` — is 875 characters. That is what the
skills now tell an agent to use for files.

`review-context` moves by ~30 characters between runs because its
`context_savings` estimate is recomputed each time; the numbers above come
from one pair of runs.

### Reproduce

```bash
R=<path to a claude-mem checkout at ade13f3, graph built with carto build>
PY=/Users/emidan/work/cartograph/engine/.venv/bin/python
git -C /Users/emidan/work/cartograph worktree add /tmp/carto-before 22d3755
for ENGINE in /tmp/carto-before/engine /Users/emidan/work/cartograph/engine; do
  for args in \
    "large-functions --min-lines 80 --limit 20" \
    "query callers_of src/services/sqlite/SessionStore.ts::SessionStore.createSDKSession --limit 20" \
    "search session --limit 20" \
    "impact --files src/services/sqlite/SessionStore.ts" \
    "dead-code --limit 20 --format json" \
    "flows --limit 20" \
    "communities" \
    "review-context --files src/services/sqlite/SessionStore.ts"; do
    printf '%s\t' "$args"
    PYTHONPATH=$ENGINE $PY -m cartograph $args --repo "$R" 2>/dev/null | wc -c
  done
done
```

Add `--detail full` to the second loop for the last column.

## What this did not fix

**`impact` is still half a megabyte by default.** `--max-results` defaults to
500 and the response carries every connecting edge — 2,507 of them here, 84%
of the compact response. Compaction cut it by 63%, but 126k tokens is not a
response any agent can use. `tests/test_token_budget.py` already records that
`changed_nodes` and `edges` ignore `max_results`. The fix is a lower default
and a bound on edges; it is a behaviour change to the tool, not to row shape,
so it is left for its own change.

**`query` edges repeat the target.** For `callers_of`, every edge ends at the
node that was asked about, so its qualified name is written twenty times
(about half of the compact response). The edge's own information is the call
site line. Not changed here: shortening an endpoint to something other than
its name needs a rule an agent can read without explanation.

## Conformance

- `query-budget` asked 500 tokens of a response that is now 358 compact, so it
  could no longer see truncation. It now asks 250; `query-budget-full` keeps
  the old 500-token case on `--detail full`. Both fail with `fit()` disabled.
- The repo-relative leak check anchored only at the start of a string, so a
  compact row carrying an absolute path after its first `|` passed. With rows
  written before relativising, `dead-code` and `search-repo-relative` still
  passed under the old check — the ops could not fail. The check now anchors
  each field of a row; the same mutation fails `dead-code`,
  `search-repo-relative` and the new `large-functions` op.
