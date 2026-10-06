---
name: debug-issue
description: Trace a bug from symptom to cause with the code graph. Use given an error, a stack trace, a failing test or a bug report. Not for a diff (review-changes) or what a change would break (refactor-safely).
---

## Debug an issue

Gets you from "here is a symptom" to "here is the code that causes it"
without reading the whole call path by hand.

### When to use this
- An error message, stack trace, failing test, or bug report.
- "Why does X happen?" / "Where does this value come from?"

### When NOT to use this
- You have a diff and want its risk → `review-changes`.
- You are orienting in an unfamiliar repo → `explore-codebase`.
- You want what a planned change would break → `refactor-safely`.

### Steps

1. **Find the entry point.** If the trace names a symbol, skip to step 2.
   A literal error string or log message lives in file contents, which the
   graph does not index — `grep -rn "<text>"` finds it exactly and cheaply.
   Search the graph for a concept or a symbol name you only half know:
   ```
   carto search "<concept or symbol name>" --limit 10 --format json
   ```

2. **Walk upstream** — who can reach this code, and therefore who can trigger
   the bug:
   ```
   carto query callers_of <symbol> --limit 20 --format json
   ```

3. **Walk downstream** — what this code depends on, and therefore where the
   bad value could come from:
   ```
   carto query callees_of <symbol> --format json
   ```
   Each row ends `calls at <lines>`: where this code makes the call. A bare
   number is a line of the row's own file; a call in another file is
   written `file:line`. `not in graph` marks a builtin or library callee.
   `references_to` is the wider net when the symbol is not a function.

4. **Check the tests** — an existing test often localises the bug faster than
   reading source, and tells you where the regression test belongs:
   ```
   carto query tests_for <symbol> --format json
   ```

5. **Before you fix it**, see what the fix would touch:
   ```
   carto impact --files <file> --depth 2 --format json
   ```
   The 20 most affected items, direct first; every affected file is in
   `data.affected_files` regardless. `truncated: true` means more items exist
   — `data.see_all` is the command that shows them.

6. **Now read the code** — the specific functions steps 1–5 identified.

`search` and `query` carry `data.coverage`. When it says code files are not
covered, the path you are tracing may run through one of them: say so, and
`grep` them before ruling a caller out.

### Useful patterns

`carto query` takes the pattern as a positional argument, then the target:
`callers_of`, `callees_of`, `references_to`, `tests_for`, `imports_of`,
`importers_of`, `children_of`, `inheritors_of`, `handlers_of`,
`endpoints_for`, `file_summary`. Full list:
`carto capabilities --command query`.

A target is a symbol name, or `path/to/file.py::name` when the name is
ambiguous. Result rows read `kind | name | path:line[ | resolution]`; join
path and name with `::` to pass a row back as the next target.

### Budget

- An empty result is a real answer, not a failure — but `grep` for the name
  before reporting that nothing calls a function.
- Stop walking the graph once you have a hypothesis you can check by reading
  one or two functions. The graph narrows the search; it does not diagnose.

### If it fails

Exit `2` means no graph — run `error.remediation` or use `build-graph`. If
the graph predates the bug's code, run `carto update --base HEAD~1` first.

### Report

State the cause, the evidence path you followed (symbol → callers → source),
the fix, and whether a test covers it today.

### Reference

`carto capabilities --format json`.
