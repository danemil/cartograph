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
| architecture | yes, since 2026-10-01 | the first call of an overview; see "`architecture`: the overview in one call" |
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

## `architecture`: the overview in one call

A measured Copilot CLI run (gpt-5.4-mini, three runs a side) asked "Give me a
short overview of how this repository is structured: its main components and
how they connect." With carto it cost 118,915 tokens a run against 63,059
without (+89%). Every run called `architecture`, `communities` and `flows`,
as the explore-codebase skill said to, and each early response is re-sent on
every later model call, so its size multiplies. The skill's first call was
`architecture --detail-level standard --max-tokens 3000`, mostly member
samples (test names) and a hundred individual cross-community edges.

### What changed

- `architecture` gets compact rows. A community row names where it lives —
  generated names like `acr-up-detect` say little on their own:
  `shared-server | 3719 nodes | typescript | src/services 52%, src/server 10%`
  (`top_dirs`: the two directories, two levels deep, most of its members are
  in). A connection row is undirected, because pairs are aggregated in
  canonical order: `scripts-fetch <-> shared-server | 103 edges | CALLS,
  REFERENCES | high coupling`. `high_coupling` is the warnings' own rule
  (`communities.is_high_coupling`), so `warnings` goes when every connection
  row is compact; `cross_community_edges_total` (in the summary), `_hints` and
  the `context_savings` estimate go too. `--detail-level standard` rows carry
  the members asked for and are left whole, and so are their warnings.
- The explore-codebase skill answers an overview with that one call, and
  drills into `community` or `flows` only for a question the overview does
  not answer.

### Measured

claude-mem at `ade13f3`, stdout characters of `--format json`, tokens as
chars/4. Before is `3caae27`.

| Overview path the skill gives | Calls | Chars | Tokens |
|---|---|---:|---:|
| Before: `architecture --detail-level standard --max-tokens 3000`, `communities --sort size --min-size 5`, `flows --sort criticality --limit 10` | 3 | 13,996 + 1,157 + 1,438 = 16,591 | 4,149 |
| After: `architecture` | 1 | 1,671 | 418 |

`architecture` on its own default: 4,211 → 1,671 chars (−60%); `--detail
full` 6,037; `--detail-level standard` 37,734. The after response names all
13 communities with their directories and the 3 coupled pairs with edge
counts and kinds — what the before path gave, less member samples, cohesion
and the per-edge list. Not re-measured: the end-to-end Copilot run.

## `architecture`: the layout, because the graph is not the repository

The one-call overview above was cheap and, on the wrong repository, wrong. A
second user-run A/B (Copilot CLI, gpt-5.4-mini, five alternating rounds) asked
the same overview question of an "AI-SDLC bootstrap kit" — docs, templates,
workflows, AGENTS.md, a few Python scripts. With carto the answer was wrong
5/5: the agent ran `architecture`, as the skill said, and described the
scripts' communities as the repository. Without carto it read the README and
was right 4/4. Before the one-call change, the agent also read files and got
it right. The graph covers parsed code; nothing in the response said so.

### What changed

`architecture` leads with `layout`, built from every tracked file — and,
since the change in the last section, every untracked one git does not ignore
(`incremental.repository_files`, the list the build itself starts from;
a walk with the ignore patterns where there is no version control). Kinds are
decided by file name alone (`layout.kind_of`), so the overview never reads a
file to count it:

```
"layout": {
  "files": 51,
  "by_kind": {"code": 7, "docs": 38, "config": 5, "other": 1},
  "graph_files": 5,
  "dirs": [
    "(root) | 4 files | docs 3, config 1 | read first: README.md, AGENTS.md",
    "docs/ | 24 files | docs 24",
    "template/ | 13 files | docs 11, config 1, other 1",
    "scripts/ | 4 files | code 4",
    ".github/ | 3 files | config 3",
    "dashboard/ | 3 files | code 3"
  ],
  "note": "86% of files are not code; the code graph covers 10%. Communities below describe only that part; read README.md, AGENTS.md for the rest."
}
```

That is the docs-heavy fixture built to the A/B repository's shape. The
`note` appears only when under half the tracked files are code. At most 12
top-level directories are listed, largest first; the rest are summed in
`dirs_omitted`. The root row names up to four root documents to read first
(README, AGENTS, CLAUDE, ARCHITECTURE, CONTRIBUTING, in that order). Rows
are compact by `layout_dir_row`; `--detail full` keeps them as objects.

The explore-codebase skill now says the graph covers parsed code only, and
that when `layout.note` is present the agent reads the files under `read
first:` and describes the repository from its layout and those docs.

### Measured

Stdout characters of `architecture --format json`; before is `1097ccc`.

| Repository | Before | After | Note |
|---|---:|---:|---|
| docs-heavy fixture (51 files, 5 in the graph) | 518 | 1,178 | yes |
| claude-mem at `ade13f3` (1,534 tracked, 991 in the graph) | 1,671 | 2,819 | no, 65% code |

claude-mem's layout is 13 rows (root plus 12 directories, 17 more summed).
The overview path is still a sixth of the three-call path it replaced
(16,591 chars). Not measured: whether the agent now reads the README on the
user's repository — that needs the A/B re-run.


## `large-functions`: top N means N, and every list says what it covered

A third user-run A/B (Copilot CLI, gpt-5.4-mini, five rounds) asked "Which
are the largest functions in this repository, by line count? List the top 10
with their file and line count." With carto 0/5 answers were fully right (four
matched 7 of 10, one listed only 6); without, 2/5 — although carto cut the
cost by 83%. Two causes, both a partial answer presented as complete:

1. `--min-lines` defaulted to 50. `--limit 10` on a small repository returned
   6 rows, and the agent reported those 6 as the top 10.
2. The graph was built from `git ls-files`, tracked files only. The user's
   repository had an untracked, not-ignored `.github/skills/…/scripts/`
   directory; the reference answer, built from the working tree, included its
   functions, and nothing in carto's response said any file had been left out.

### What changed

- **The working tree.** `incremental.repository_files` — the one list the
  build, `architecture`'s layout and the coverage line start from — is now
  tracked files plus `git ls-files --others --exclude-standard`
  (`get_untracked_files`): `.gitignore`, `.git/info/exclude` and the global
  excludes still keep files out, and the build's own generated/vendored
  patterns still apply on top. A second git call rather than one
  `--cached --others` call, because `--recurse-submodules` works only for
  tracked files; submodule handling is unchanged.
- **Update.** `git diff <base>` never lists an untracked file, so
  `incremental_update` adds the untracked files that are new to the graph or
  whose hash differs from the stored one (`_untracked_needing_parse`);
  unchanged ones are not queued, since every queued file also re-parses its
  dependents. A deleted untracked file goes through the existing stale-file
  reconciliation; one the user has since git-ignored is removed by one
  `git check-ignore --stdin` over the graph's files (tracked files are never
  reported by it). `get_changed_files` includes untracked files by default,
  so `detect-changes`, `review-context` and `impact`'s default change set see
  new files; update asks for the diff alone and adds its filtered set.
- **Top N.** `large-functions` has no threshold unless `--min-lines` is
  given; `--limit` (default 20, was 50) alone decides how many rows come
  back. Default when neither is given: the 20 largest — the question it
  answers is a ranking, and 20 rows is ~2.8 KB on claude-mem. The summary
  says what was applied: `Top 10 of 7481 functions by line count (--limit
  10, no --min-lines); largest: …`, or `12 functions >= 80 lines; showing 10
  (--limit 10); …`. `data.matching` is the exact count before the limit and
  feeds `page.total_estimated`, so `has_more` is exact.
- **Coverage.** `large-functions`, `search`, `query`, `refactor` and
  `dead-code` carry `data.coverage` (`layout.coverage`): every code file in
  the working tree (by name, `layout.kind_of`) plus anything else the graph
  parsed, and those it does not hold counted by reason, extensions capped at
  three:

  ```
  searched 70 of 73 code files; not covered: 2 no parser (.bat, .html), 1 generated or vendored
  ```

  Reasons: `no parser`, `generated or vendored` (`is_generated_file`),
  `excluded by ignore rules` (`.cartographignore`, nested build output),
  `symlinks`, `not in the graph (run carto update)` — new since the build, or
  failed to parse. Graph files since deleted are a separate trailing clause.
  When anything is not covered the summary ends `N of M code files not
  covered (see coverage)`, because the summary is sometimes all an agent
  reads. No file is read to compute it (bar a shebang sniff for an
  extension-less one); docs and config are not counted as gaps.
- **Skills.** explore-codebase step 5 is `large-functions --limit 10`, with
  `--min-lines` only for "every function over N lines"; a new "Coverage"
  section, and short notes in refactor-safely and debug-issue, tell the agent
  to say what was not covered rather than present the list as complete.
  Descriptions unchanged.

### Measured

claude-mem at `ade13f3`, in two copies of the checkout; before is `3f9c4e5`
(v0.8.7). It has no untracked files, so the graph is the same: full build
14.8 s → 14.4 s, 16,967 nodes and 991 files both sides. `git ls-files
--others --exclude-standard` takes ~0.01–0.02 s there; `layout.coverage`
0.05–0.06 s. With one untracked `.github/skills/deck/scripts/render.py`
(a 1,202-line function) added to both copies: `carto update` before — 0
files updated, `large-functions` still topped by a 988-line function; after —
1 file updated (4.9 s with post-processing), `render_deck` first. A no-change
update after it: 0.27 s before, 0.32 s after.

Stdout characters of `--format json`:

| Command | Before | After |
|---|---:|---:|
| `large-functions` (default) | 5,548 (50 rows, ≥ 50 lines) | 2,803 (20 rows) |
| `large-functions --limit 10` | 1,572 | 1,798 |
| `large-functions --min-lines 80 --limit 20` | 2,577 | 2,784 |
| `search session --limit 20` | 3,029 | 3,168 |
| `dead-code --limit 20 --format json` | 1,918 | 2,049 |
| `query callers_of …SessionStore.createSDKSession --limit 20` | 7,210 | 7,356 |

The coverage field there is `searched 991 of 996 code files; not covered: 5
no parser (.html)` — 64 characters, plus the summary clause.

**Not measured.** A repository with a large untracked, not-ignored tree
(say, an unignored `node_modules`): `ls-files --others` then walks it, and
the build parses whatever in it has a parser — the generated/vendored
patterns still drop `node_modules`, `dist`, `vendor` and the like. The A/B
itself has not been re-run.

### Tests, and that they can fail

`engine/tests/test_working_tree_coverage.py` (20 tests, real `git init`
repositories). Each behaviour was removed on purpose and the suite run:

| Removed | Fails |
|---|---|
| untracked files from `repository_files` | build, listing, submodule-recursion build, coverage (4), deleted-untracked removal, no-requeue |
| `--exclude-standard` | ignored-not-built, listing, detect-changes set |
| untracked files from update | new untracked picked up, edited untracked re-parsed |
| the hash filter on untracked | unchanged untracked not queued |
| `git check-ignore` in reconciliation | removed once ignored |
| stale reconciliation | deleted untracked removed, removed once ignored |
| untracked in `get_changed_files` | detect-changes set |
| `--min-lines` default 50 / `--limit` default 50 | top-N, default top 20 |
| explicit `--min-lines` ignored | explicit threshold filters |
| "no --min-lines" in the summary | top-N |
| generated / ignore-rule / pending reasons, example cap, summary clause | counts-by-reason, example cap |
| coverage on search/query/refactor, on dead-code | the per-command test |

## The fourth A/B: test functions, a moved checkout, and `--format` on writes

The user re-ran T3 on 0.8.8 (Copilot CLI, gpt-5.4-mini, five alternating
rounds, the repository copied to `/tmp/carto-ab4/with` and `…/without`).
Three defects, none of them a cost problem:

1. **Test methods were not functions.** 4/5 runs with carto ran
   `large-functions --limit 10` and missed the same two reference entries,
   both test methods (`AppSmokeTest.test_main_runs_with_tz_aware_commit`, 68
   lines; `IssueChainTests.test_issue_links_to_commit_code_and_story`, 47).
   The graph stores them as kind `Test`, and 0.6.0 made the default `Function`.
2. **A moved checkout reported itself uncovered.** The graph's paths were
   absolute, from where it was built; `coverage` compared them with the
   working tree at the new path and said `searched 0 of 71 code files` over
   results that were complete. All four answers warned the user the ranking
   was partial. The mechanism worked; the data was wrong.
3. **`carto update --format json`** was a usage error, though the README said
   every command takes `--format`.

### Why the 0.6.0 default changes

0.6.0 chose `Function` because the measured failure was rows that were not
functions at all: 15 File, 4 Class and 1 Test node in a top 20. The reasoning
was about File and Class nodes — a file or class always outranks what it
contains. A test method is not a container; it is a function that happens to
be a test, and the question "the largest functions" includes it. What *is* a
container among Test nodes is a JS/TS `describe`/`suite` block: it wraps a
spec file the way a class wraps methods, and on claude-mem those blocks were
18 of the 20 largest Test nodes (up to 1,718 lines). So the default is now
`Function` and `Test` minus `describe`/`suite` blocks; `it`/`test` blocks,
Python test functions and methods, JUnit `@Test` methods are ranked. Rows
already carry the kind (`68 lines | Test | AppSmokeTest.test_main… | …`); the
summary says `functions (tests included)`; `--kind Test` lists every Test node
including suites, `--kind Function` excludes tests.

Other commands checked for the same exclusion: `search`, `query` and `impact`
never filtered Test out. The Function-only filters that remain are deliberate:
`dead-code` and `refactor dead_code` (a test has no callers by design),
`refactor suggest` (cross-community moves), and review guidance's "changed
functions lacking tests" (a test is not a gap in its own coverage). None
changed.

### A moved or remounted checkout

The same failure hits a Dev Container that mounts a checkout at
`/workspaces/<name>` while the host built the graph at its own path. What a
copy did before the fix, on a two-file fixture with the original path gone
(`<old>` is the build path):

| Command | 0.8.8 | Now |
|---|---|---|
| `status` | correct | correct |
| `large-functions` | results right; `coverage: searched 0 of 2`, summary "2 of 2 code files not covered"; rows `<old>/pkg/core.py:5` | `searched all 2 code files`; rows `pkg/core.py:5` |
| `search` | same false coverage; absolute old paths | full coverage; relative |
| `query callers_of pkg/core.py::compute` | answered (search fallback found it), false coverage, absolute rows | exact, relative |
| `query importers_of pkg/a.py` | empty | found |
| `impact --files pkg/core.py` | answered (suffix match), absolute old paths in every row | relative |
| `architecture` | `layout.graph_files: 0` | `2` |
| `detect-changes` (after an edit) | "0 changed function(s)" | the edited functions |
| `review-context` | changed nodes right, old absolute paths | relative |
| `flow --source` | no source (read from the old path; or another checkout's copy if it still exists) | this checkout's source |
| `mem search` | correct — observations store repo-relative paths | unchanged |
| `update` | refused: "built with a different repository root" (exit 1, text only) | rebases, then updates |

**Reads** compare repo-relative paths. `repo_paths.register_anchor` works out,
once per graph and root, which root the stored paths are under: none if any
stored File path is under the current root; otherwise the prefix that,
removed from a stored path, leaves a file that exists here, voted over 200
File paths and accepted only if at least half agree — so the graph of some
other repository that shares a few file names is never adopted. `relativise`,
`layout` (coverage and the architecture layout), query targets,
`changes.analyze_changes` and flow source reads consult it. Nothing is
written on a read: two mounts of one `.cartograph/` would otherwise rewrite
the graph back and forth on every query.

**Writes** (`update`, `build`) first move the stored paths to the root they
run at — `GraphStore.rebase_paths`, a prefix `REPLACE` over every text column
of every table (node and edge identity, File signatures, `extra`, flow and
risk snapshots, embedding keys) in one transaction, then an FTS rebuild — so
new rows and old rows share one spelling. The schema is unchanged; existing
graphs need no migration step, they are rebased by the first write. A graph
that does not map onto the tree is still refused, as before.

### `--format` on the commands that write

`build`, `update`, `postprocess` and `embed` take `--format json|text`
(default text, as `status` does) and `--max-tokens`. In json mode stdout is
the envelope alone — anything printed during the run is redirected to stderr
— and `data` is the summary plus counts and the files touched (`build_type`,
`rebuild_reason` — present only when an update rebuilt in full because an
older parser built the graph, `parser_version`, or an older C++ identity
format, `cpp_identity`; `build_type` is then `full` —
`files_parsed`/`files_updated`, `total_nodes`, `total_edges` — the whole
graph after the run, for update as for build, and the build summary quotes
the same final totals — `nodes_updated`/`edges_updated`
(what an update re-parsed), `changed_files` — the changed files the update
applied to the graph, re-parsed or removed as deleted, not the raw diff —
`ignored_changes` — changed files it left alone: ignored, not code, or the
same content; a file that failed to parse is in `errors` instead —
`dependent_files`, `base_resolved`, `errors`, `warnings`);
postprocess and embed carry their result whole. Exit codes: `0` ok; `2`
precondition with a remediation for no git repository (`update`), no graph
(`postprocess`, `embed`), a graph of another repository, and an embedding
provider that is not configured; `3` anything else. Text mode is unchanged.

### Graphs built by an older parser (0.9.4)

A full build records `parser_version` (`incremental.PARSER_VERSION`) in the
graph's metadata; it is bumped whenever a parser change alters the nodes or
edges of unchanged code. The next `update` on a graph with nodes whose stored
value differs or is missing — every graph before 0.9.4 — rebuilds it in full
once, reported as above. Until then `status` says so: `data.stale` is true,
`data.stale_reason` is `"built by an older Cartograph parser"` and
`data.remediation` is `"carto build"` (both null on a current graph; a branch
change gives its own reason; several are joined with `; `). The text form
prints a WARNING line. Status only reads the value, never writes it.

Not given `--format`: `visualize`, whose `--format` already chooses an export
format (html, json, graphml, …) — an envelope flag would collide with it —
and `wiki`, `forget` and `repos`, which print for a person and which no skill
runs. The README and the `carto capabilities` convention now name these
instead of claiming every command.

### Measured

claude-mem at `ade13f3`, graph from the earlier session (16,967 nodes, 991
files). Stdout characters of `large-functions --format json`, 0.8.8 → now:
`--limit 10` 1,798 → 1,818; default 2,803 → 2,823; `--min-lines 80 --limit
20` 2,784 → 2,801. The rows are identical — no test function is in claude-mem's
top 20 once suites are left out — and the +17–20 characters are
"(tests included)". The count of candidates went 7,481 → 12,880.

A full copy of the same checkout at a new path: `large-functions --limit 10`
1.06 s at the original, 1.23 s in the copy (the anchor inference, one
`stat` per sampled path); coverage `searched 991 of 996 code files; not
covered: 5 no parser (.html)`, identical to the original. The first `update`
in the copy, which rebases, took 9.7 s; the next 0.33 s. Afterwards `sqlite3
.dump` holds no occurrence of the old prefix and FTS answers.

**Not measured.** The A/B itself has not been re-run. The rebase cost is paid
again each time a *write* happens at the other mount (host and container both
updating one shared `.cartograph/`): ~10 s on claude-mem per switch. Nothing
was run in a real Dev Container.

### Tests, and that they can fail

`engine/tests/test_large_functions.py` (7 new), `engine/tests/test_moved_checkout.py`
(14, real `git init` repositories copied with `shutil.copytree`, the original
deleted unless the test is about two mounts), `engine/tests/test_build_update_envelope.py`
(11, the CLI as a subprocess, envelopes validated against the schema), and
conformance cases `build`, `update`, `postprocess`. Each behaviour was removed
and the suite run:

| Removed | Fails |
|---|---|
| `Test` from the default kinds | default ranks tests, summary says so, CLI rows, parsed test methods, and the two older summary tests |
| the describe/suite filter | suites are not functions, default ranking, CLI rows, two pre-existing default tests |
| "(tests included)" in the summary | three summary tests here, two in `test_working_tree_coverage.py` |
| the anchor in `layout` | coverage after a move, coverage with both paths, architecture layout, two mounts |
| the anchor in `relativise` | relative rows, impact, review-context, two mounts |
| the anchor for query targets | importers_of after a move |
| `register_anchor` in `_get_store` | importers_of, architecture, impact, review-context, flow source |
| `register_anchor` for the non-tool commands | detect-changes |
| the rebase before writing | update after a move, unchanged files keep edges, two mounts |
| the half-must-agree rule | another repository's graph is not adopted |
| the anchor in `analyze_changes` | detect-changes |
| the working-tree path for flow source | flow source |
| stdout redirected during a build | anything printed goes to stderr |
| RuntimeError → precondition | another repository's graph (update) |
| no-git → precondition | update outside git |
| an error result → precondition | embed with an unconfigured provider |
| the no-graph checks | postprocess / embed without a graph |
| `--format` on update / the json branch of postprocess | every update / postprocess test |

Edits made while working that no test could make fail were taken out again,
because existing paths already covered them: anchored candidates in `impact
--files` (its suffix match finds the file), in `importers_of` with an
unresolved node, in `review-context`'s and the minimal-context tool's own path
joins (both go through `analyze_changes`), anchor registration in the
graph-tool dispatcher and in `_attach_coverage` (every tool opens through
`_get_store`), and relativising build results (their paths are already
repo-relative).

## The fifth A/B: what is inside a dominant directory, and what an impact row depends on

The user's A/B on 0.8.9 (Copilot CLI, gpt-5.4-mini, five alternating rounds,
isolated homes; vault `20 Projects/FRQ/report6.md`) found two answers that
carto made worse, not cheaper:

- **T2, the overview:** with carto 2/5 correct, 3 partly (without: 4/5).
  Every run called `architecture` alone. Two partly answers left out the
  role-playbook skills (`template/.claude/skills/*`) and the MCP server
  (`template/scripts/knowledge/mcp_server.py`), both nested in `template/`,
  which the layout showed as one row of counts. One explained connections by
  community coupling instead of what the README says flows where.
- **T5, callers and blast radius:** with carto 4/5 correct, 1 partly; +20%
  billing. The partly answer called every importer of `graph_store.py` a
  "directly impacted runtime path" of a change to `add_node`: impact's rows
  said `direct | File | ingest_docs.py` for an importer exactly as they said
  `direct | Function | ingest` for a caller. One run also tried `carto query
  impact …`, a usage error that named the sixteen patterns and nothing else.

### A dominant directory names what is inside it

A top-level directory is expanded when it holds **at least 25% of all files,
or at least 50% of all code files**, is not itself a test tree, and has
sub-directories. A quarter of the files is a part too big for "it holds most
of the repository" to describe; at most four directories can reach it, which
bounds the cost. The code share catches the A/B's shape: a kit that is
mostly docs, whose implementation lives in one directory that holds only part
of the files. A top-level `tests/` is one component, the tests; its
sub-directories mirror the code, and a `tests/server/` in it is not a server.

The expanded row adds `sub:` (second-level directories, largest first, at
most 6, with counts and kinds; the rest summed; the directory's own files
counted) and the components found anywhere inside it, by path alone:

| Component | Recognised by |
|---|---|
| `skills:` | a `skills/` directory whose children hold `SKILL.md`, with the count |
| `server:` | a code file whose name says server or MCP (`*server*`, `mcp_*`), not a test; a `server/`/`mcp/` directory |
| `app:` | a `dashboard/`, `app/`, `apps/`, `web/`, `frontend/`, `ui/`, `site/`, `viewer/` directory |
| `tests:` | the outermost `tests/`, `test/`, `__tests__/`, `spec/` directory, with its file count |
| `hooks:` | a `hooks/` directory |
| `ci:` | a `.github/workflows/` directory, with its file count |

Each kind names three, shallowest first, and counts the rest. On a fixture
built to the A/B repository's shape (103 files, `template/` 66 of them):

```
template/ | 66 files | code 24, docs 38, config 3, other 1 | sub: docs/ 20 (docs 20), .claude/ 19 (code 2, docs 16, config 1), scripts/ 19 (code 19), dashboard/ 4 (code 3, docs 1), .github/ 2 (config 2); 2 own files | skills: template/.claude/skills/ (8); server: template/scripts/knowledge/mcp_server.py; app: template/dashboard/; tests: template/scripts/knowledge/tests/ (5); hooks: template/.claude/hooks/; ci: template/.github/workflows/ (2)
```

The explore-codebase skill now says to name every component the layout
names, nested ones included, and to take the connections between them from
the README, the docs and the entry points — "a workflow runs the validators,
ingestion writes the store a server reads" — not from community coupling,
which says only that code shares edges.

### An impact row says how it depends on the change

Every row now carries the relation after `direct`/`transitive`:

```
before  direct | Function | ingest | kn/ingest_code.py:4
        direct | File | kn/link_commits.py
        transitive | Function | main | kn/cli.py:4
after   direct | calls add_node | Function | ingest | kn/ingest_code.py:4
        direct | calls get_node | Function | lookup | kn/query.py:4
        direct | imports graph_store.py | File | kn/link_commits.py
        transitive | calls ingest | Function | main | kn/cli.py:4
```

A direct row names its edges to the changed nodes, grouped by verb, three
names a verb, strongest first. A transitive row names the hop on its best
path — the one its score was ranked by. `--detail full` rows carry the same
as `via`. The summary splits the direct count by each dependent's strongest
relation, over every direct dependent rather than the rows shown, and says
what an import-only dependent means:

```
before  graph_store.py: 11 items affected within 2 hops across 6 files (9 direct); all shown
after   graph_store.py: 11 items affected within 2 hops across 6 files (9 direct: 4 call, 5 import only); all shown; the 5 import-only dependents are not broken by a signature change, only by a rename or removal; callers of one symbol: carto query callers_of kn/graph_store.py::<name>
```

`totals.direct_by_relation` carries the split, and a file row says how many of
its direct items only import (`kn/link_commits.py | 1 item (1 direct; 1 only
imports)`). Everything 0.6.1 guarantees is unchanged: exact totals, every
affected file listed, direct first, truncation flagged. The relations are
computed by one helper both traversal engines call (`GraphStore._impact_relations`),
from the edges table, in the traversal's own orientation; `review-context`,
which uses the same traversal, does not ask for them and does not pay for them.

The refactor-safely skill now answers a signature question from `query
callers_of` and `query tests_for` and says to run `impact` only for "what
else could be affected", with how to read its rows: it starts from every
symbol in the file, `calls get_node` is not a caller of `add_node`, imports do
not break on a signature change, transitive rows are callers of callers.

### A wrong spelling names the right command

argparse rejects `carto query impact`, `query large-functions`, `query
callers` and a bare `carto callers_of`, and listed only the valid choices.
The usage envelope (still exit 1) now leads its message with the command
meant and carries it, rewritten from the agent's own arguments, as
`error.remediation`:

```
carto query impact --files kn/graph_store.py --depth 2 --format json
  -> remediation: carto impact --files kn/graph_store.py --depth 2 --format json
     message: `impact` is a command of its own, not a query pattern: run `…` (callers of one symbol: `carto query callers_of <file>::<name>`). argument PATTERN: invalid choice: …
carto query impact kn/graph_store.py::add_node     -> carto impact --files kn/graph_store.py (message names callers_of for the symbol)
carto query large-functions --limit 10              -> carto large-functions --limit 10
carto query callers add_node                        -> carto query callers_of add_node
carto callers_of add_node                           -> carto query callers_of add_node
```

### Measured

Stdout bytes of `--format json`. Before is `2539881` (0.8.9 + docs), run from
a worktree; after is this change. claude-mem is a fresh shallow clone at
`1bb6439` (2,023 tracked files; the graph from earlier sessions was gone).

| Command | Before | After |
|---|---:|---:|
| `architecture`, A/B-shaped fixture (103 files, `template/` 66) | 1,300 | 1,769 |
| `architecture`, docs-kit fixture (51 files) | 1,170 | 1,345 |
| `architecture`, claude-mem (no directory qualifies: `tests/` is a test tree, `src/` 23% of files, 32% of code) | 2,906 | 2,906 |
| `impact --files kn/graph_store.py`, callers fixture | 1,833 | 2,437 |
| `impact --files src/services/sqlite/SessionStore.ts`, claude-mem | 18,369 | 21,997 |
| `impact --files src/services/worker/knowledge/CorpusBuilder.ts` | 4,686 | 5,801 |
| `impact --files src/utils/logger.ts` (817+ direct, the hub) | 55,111 | 61,168 |

Impact grows 11–33%: the relation on each of the 20 rows and two summary
clauses. That is the price of a row that cannot be misread; the file list,
which is most of a hub's response, is unchanged but for the `only imports`
counts. Wall time on logger.ts: 1 s before and after, timed in whole seconds.

### Tests, and that they can fail

`engine/tests/test_layout.py` (7 new, one a real `git init` + build),
`engine/tests/test_impact_edges.py` (11, a Python repository parsed by the CLI's
own build: a caller through `from … import`, one through `import module`, a
caller of another function in the file, an import-only file, a test, a
caller of a caller), two in `test_skills.py`, and the summary test in
`test_impact_scope.py` updated for the split. Each behaviour was removed and
the tests run:

| Removed | Fails |
|---|---|
| expansion of a dominant directory | sub-directories, components, code share, compact row, architecture e2e, test tree |
| the code-share criterion | expanded however small |
| the test-tree exclusion | test tree is not expanded |
| test files excluded from servers | components, compact row |
| skills detection / app detection | components, compact row, architecture e2e |
| `sub:` / components in the compact row | compact row (and e2e) |
| the relation in an impact row | each row says how it depends |
| `only imports` in a file row | file rows |
| the import-only clause / the callers_of clause in the summary | summary separates callers from importers |
| relations over the kept rows only | breakdown beyond the limit, the 0.6.1 summary test |
| the transitive hop | each row, full rows |
| relations on the networkx engine | both engines agree (old and new) |
| the corrected call in the parser's `error` | all five wrong-spelling tests |
| `--files <path>` from a positional symbol | query impact on a symbol |
| bare pattern as a command | pattern used as a command |
| the new explore-codebase / refactor-safely text | the two skill tests |

Not measured: the A/B itself on this version — whether the overview now names
the skills and the server, and whether a T5 answer now keeps importers apart
from callers, needs the user's re-run.
