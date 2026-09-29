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
| large-functions, query, search, impact, dead-code, flows, communities, review-context | yes | measured below; `impact` has its own section |
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

**`impact` was still half a megabyte by default.** Fixed separately; see
"`impact`: short, ranked, and never short on scope" below.

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

## `impact`: short, ranked, and never short on scope

Compaction took `impact --files src/services/sqlite/SessionStore.ts` from 1.36 MB
to 506 KB — still ~126k tokens, because it listed 500 nodes and every edge
among them (2,507 edges, 84% of the response). The fix is a lower default. The
constraint the user set on it: a short list must never let an agent believe
it has seen everything. The failure being designed against is an agent that
changes a signature, reads twenty rows, and breaks a caller it was never
shown. So the response got smaller by listing fewer *items*, and nothing else
about the scope was shortened.

### What the default says

```
summary          SessionStore.ts: 551 items affected within 2 hops across 99 files
                 (399 direct); showing top 20; 399 direct dependents; 20 shown;
                 --limit 399 lists them all
totals           {items: 551, direct: 399, files: 99, changed_nodes: 127,
                  edges: {CALLS: 1094, CONTAINS: 454, IMPORTS_FROM: 201, ...}}
changed_files    the files asked about
impacted_nodes   20 rows: "direct | Function | SearchManager.timeline | src/.../SearchManager.ts:746"
affected_files   EVERY affected file: "tests/sqlite/session-store-prompts.test.ts | 18 items (18 direct)"
see_all          carto impact --files src/services/sqlite/SessionStore.ts --depth 2 --limit 551 --detail full
```

- **Every affected file is listed**, with its item count and how many of those
  are direct, whatever `--limit` is. This is the completeness guarantee: the
  file set of the default response equals the file set of the complete one.
  Files with a direct dependent come first, then by count.
- **Totals are exact** and computed over everything reachable, not over the
  rows kept: items, direct dependents, files, the changed file's own nodes,
  and the connecting edges counted by kind — the same edges `--detail full`
  lists.
- **Truncation reuses the envelope's fields** rather than adding a `complete`
  flag: `truncated: true` with `truncated_reason: "page_limit"` exactly when
  items were left out, and `page.total_estimated` carries the exact total (the
  name says "estimated"; for `impact` it is exact). `page.has_more` is now
  computed from that total — before, a response holding every row at exactly
  the limit advertised a next page. `data.see_all` is the command that lists
  everything, written out whole so an agent runs it rather than reconstructs it.
- **The edge list and the changed file's own nodes** are counts in the
  default and lists under `--detail full`. The changed nodes are the contents
  of the file the caller named, not affected items.
- `--detail full` keeps the whole rows, `changed_nodes` and `edges`; it does
  not change how many items are listed — `--limit` does, as on every other
  list command. The flag is now `--limit` (default 20), not `--max-results`
  (default 500), for the same reason.

### The ranking

Direct dependents first; then the best-path impact score; then qualified name.
Deterministic, and the same on both traversal engines (a test runs both).

"Direct" is one hop from a changed node along an edge the traversal follows —
a caller, an importer, an inheritor, a reference, or a test (`TESTED_BY`) —
recorded during the traversal's first step. The score alone is the wrong first
key: it multiplies edge weight by distance, so a direct importer (0.5 x 0.6 =
0.30) ranked below a caller two calls away (0.6 x 0.6 = 0.36). The direct one
is the one whose code breaks when a signature changes. The cap is applied to
this finished ranking in SQL (`ORDER BY direct DESC, score DESC, name`), so
the kept rows are the top of the true order, not a re-sort of a score-capped
set.

### Direct dependents do not get a cap of their own

Considered: always list every direct dependent, and cap only the transitive
ones. Rejected because the count is unbounded — `logger.ts` in claude-mem has
817 direct dependents, which would be ~60,000 characters of rows before the
first transitive one, i.e. the original problem again on exactly the files
most likely to be changed carelessly. Instead:

1. direct dependents rank first, so the 20 shown are direct whenever there are
   20 or more of them;
2. when direct dependents outnumber the rows, the summary says so in words
   ("399 direct dependents; 20 shown") — the line an agent reads first, and
   sometimes reads alone;
3. because they rank first, `--limit <totals.direct>` returns *exactly* the
   direct set, and the summary names that limit;
4. `affected_files` gives each file's direct count, so every file holding a
   direct dependent is named even when its rows are not shown.

Nothing direct can be dropped silently: it is either a row, or counted in the
summary, in `totals.direct`, and against its file.

### Measured

claude-mem at `ade13f3` (991 files, graph already built), stdout characters of
`--format json`, tokens as chars/4 (ceil). "Before" is `f72bfdf` (v0.6.0);
"complete" is the `see_all` command. The small file's default is already
complete, so it has no `see_all`.

| File | Scope | Before | After (default) | After `--detail full` | Complete (`see_all`) |
|---|---|---:|---:|---:|---:|
| `src/services/sqlite/SessionStore.ts` | 551 items, 399 direct, 99 files | 506,392 (126,598 tok) | **10,967 (2,742 tok)** | 220,609 (55,153 tok) | 1,473,724 (368,431 tok) |
| `src/services/integrations/TelegramWrapupNotifier.ts` | 121 items, 40 direct, 38 files | 78,826 (19,707 tok) | **7,038 (1,760 tok)** | 68,025 (17,007 tok) | 221,773 (55,444 tok) |
| `src/services/worker/knowledge/CorpusBuilder.ts` | 14 items, 5 direct, 12 files | 8,619 (2,155 tok) | **3,052 (763 tok)** — all shown | 26,601 (6,651 tok) | same as `--detail full` |
| `src/utils/logger.ts` (a hub, for the upper bound) | 2,376 items, 817 direct, 463 files | 285,890 (71,473 tok) | **34,386 (8,597 tok)** | 138,485 (34,622 tok) | 4,302,778 (1,075,695 tok) |

**File sets.** For all four, the default's `affected_files` equals the set of
`file_path`s of every item in the complete response, and equals the complete
response's `impacted_files` (99, 38, 12 and 463 files). Totals match the
complete response too: item and direct counts, and `sum(totals.edges)` equals
its edge count (2,680; 357; 35; 7,910).

**The old default understated scope.** Its `impacted_files` was built from
the 500 nodes it kept, so for SessionStore.ts it named 79 of 99 files, and for
logger.ts 141 of 463 — with a summary saying "in 79 other files". That
understatement is the hazard this change exists to remove, and it predates it.

**The guarantee has a price on hub files.** logger.ts's default is 34 KB, of
which the 463 file rows are nearly all. That is the cost of never shortening
the file list; it is ~12% of the old default and ~0.8% of the complete answer.

### Reproduce

```bash
R=<path to a claude-mem checkout at ade13f3, graph built with carto build>
PY=/Users/emidan/work/cartograph/engine/.venv/bin/python
git -C /Users/emidan/work/cartograph worktree add /tmp/carto-before f72bfdf
for F in src/services/sqlite/SessionStore.ts \
         src/services/integrations/TelegramWrapupNotifier.ts \
         src/services/worker/knowledge/CorpusBuilder.ts \
         src/utils/logger.ts; do
  PYTHONPATH=/tmp/carto-before/engine $PY -m cartograph impact --files $F --repo "$R" --format json | wc -c
  PYTHONPATH=/Users/emidan/work/cartograph/engine $PY -m cartograph impact --files $F --repo "$R" --format json | wc -c
  PYTHONPATH=/Users/emidan/work/cartograph/engine $PY -m cartograph impact --files $F --repo "$R" --format json --detail full | wc -c
  # complete: the data.see_all command of the default response
  PYTHONPATH=/Users/emidan/work/cartograph/engine $PY -m cartograph impact --files $F --repo "$R" --format json --limit <totals.items> --detail full | wc -c
done
```

### Tests, and that they can fail

`engine/tests/test_impact_scope.py` builds a graph whose answer is counted by
hand: one changed function, 25 direct callers across 13 files, a caller of
each (25 transitive), and one file that only imports the changed file — whose
score (0.30) is below every transitive caller's (0.36), so "direct first" is
observable. Directory names are chosen so that ordering by name alone cannot
pass for the ranking. Each guarantee was broken on purpose and the tests run:

| Removed | Fails |
|---|---|
| file counts over the kept rows only | totals-equal-complete, every-affected-file, file-list-independent-of-limit, summary |
| direct total over the kept rows only | totals-equal-complete, direct-beyond-limit announced, summary |
| edge counts | totals-equal-complete |
| `truncated` never set | truncated-when-one-left-out, see-all-shows-everything |
| `has_more` as `len >= limit` | not-truncated-when-all-shown |
| the "N direct dependents; M shown" clause | direct-beyond-limit announced |
| direct-first in the sort key, or in the SQL cap | direct-rank-before-higher-scoring |
| networkx engine ordering differently | both-engines-agree |
| name as the tie-break | equal-scores-ordered-by-name |
| `see_all` | truncated-when-one-left-out, see-all-shows-everything |
| compact keeping `edges` | default-carries-counts-not-edges |

Conformance gained one op, `impact` (`--limit 3` on this repository's
`graph.py`): page collection, limit, count and `has_more`, and no absolute
paths in `data`. It fails on the previous CLI (no `--limit`: exit 1, no page
block), with `affected_files` rows left unrelativised (absolute-path check),
and with `has_more` forced false.
