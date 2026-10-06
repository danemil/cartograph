# Cartograph changelog

Releases of Cartograph itself. `engine/CHANGELOG.md` is the upstream
code-review-graph history the engine was forked from (2.3.8).

## 0.9.7 — 2026-10-06

- **Flows and communities stay current after `carto update`.** An
  incremental update compared repo-relative paths with stored absolute ones,
  so it re-traced nothing: a new function got no flow and no community until
  the next full build, and re-parsed nodes left flows pointing at nodes that
  no longer existed. Now an update gives the same flows and communities as a
  full rebuild (new entry points, changed and deleted files, a function left
  without callers). Cost: about 0.04 s per update on a 450-node repository;
  roughly 1–3 s on a 25k-node one, the same as a full post-process.
- Community detection no longer depends on the order nodes were stored in
  (Leiden, when `igraph` is installed: assignments shift once).
- **`callees_of` reads like `callers_of`**: one row per callee with every call
  line (`called at ingest.py:75, 92`), and both totals in the summary
  ("23 callee(s), 37 call line(s)" — it used to drop 14 of those 37).
- Compact `callers_of` / `callees_of` output leaves out `edges`, which only
  repeated the rows (about 35–50% smaller); `--detail full` keeps them.
- A paged query carries only its own page's edges (page 2 used to repeat
  page 1's).

## 0.9.6 — 2026-10-06

**Every graph rebuilds once** on the first `carto update` (parser version 3).

- **A test file is judged by its path inside the repository.** The patterns
  were matched against the absolute path, so a checkout under a folder such as
  `dc-test/` or `latest/` made every file a test file: `tests_for` returned
  production callers as test helpers (11 tests instead of 5 in a Dev
  Container at `/workspaces/dc-test`), and JS production code could become
  Test nodes. Directory patterns are now anchored to whole path components;
  flows and dead code use the same classification as the parser, so helpers
  under `tests/`, `test-utils/`, `_test.go` and `FooTest.java` count as test
  code there too.
- `carto mem search "<query>"` works; `--query` still does.
- Dev Container setup, as checked by hand: `docs/verify-memory.md`, step 10.

## 0.9.5 — 2026-10-06

**Every graph rebuilds once** on the first `carto update` (parser version 2);
`carto status` says stale until then.

Java and Go call binding, from PR #1 (an outside contributor), finished here:

- A call binds by its receiver, the class hierarchy and the variable's
  declared type, instead of to the caller's own method of the same name.
  `super.m()` is no longer read as the override calling itself.
- A call through an interface or base class reaches the implementation:
  `callers_of` lists it as `via_supertype`, and `impact` on an implementation
  finds the callers of its interface (it used to report nothing affected).
- Two calls of the same name on one line (`repo.save(); other.save();`) are
  kept apart in every language. Before, the store kept one of them.
- The frozen engine no longer crashes with `BrokenProcessPool` on macOS and
  Windows when a repository has 8 or more files.
- Responses no longer carry `_hints` that name MCP tools.

Changed for every language, not only Java and Go:

- **`callers_of` lists uncertain callers, labelled.** A caller matched by name
  ends `unresolved`, one of several candidates `ambiguous`. 0.9.4 left these
  out. The refactor-safely skill tells agents to report them apart and read
  the call line first.
- **No guessing by elimination.** A call on another object (`other.save()`)
  is no longer bound to the caller's own class's `save`. Outside Java, a
  candidate left only by ruling others out stays labelled, not resolved.
- **Each caller row names its call lines**: `… | test_graph_store.py:18 |
  calls at 20, 22`, and the summary gives both totals ("3 caller(s), 7 call
  line(s)"). `--detail full` has `call_lines`.
- The keyword fallback of `carto search` (used only when full-text and
  semantic search find nothing) matches any word of a multi-word query and
  ranks rows matching more words first.

The contributor measured Java recall 0.82–0.87 → 0.97–1.00 and Go
0.937 → 0.998 against javac and Go's call-graph tool. Those figures are theirs
and were taken before the fixes above; they have not been re-measured here.
What was verified here: the Java cases in `engine/tests/test_java_binding.py`,
and identical `callers_of` results on 60 sampled Python functions.

## 0.9.4 — 2026-10-05

- A graph built by an older parser is rebuilt in full by the next `update`;
  `status` says stale, "built by an older Cartograph parser", until then.
- `callers_of` lists every call line in `edges`, not only the first per caller.
- `update`: `changed_files` lists what was applied; other changes are
  `ignored_changes`. The build summary quotes the final totals.
- refactor-safely: what a signature change breaks, per kind of change.

## 0.9.3 — 2026-10-05

- `tests_for` finds tests that call through `import x as y`, and tests that
  reach the symbol through a helper in the test file (`via <helper>`).
- `update` reports the whole graph's `total_nodes` / `total_edges`.
- `install` and `uninstall` remove the Claude Code hooks releases before 0.6.0
  wrote to `.claude/settings.json` (exact commands only).
