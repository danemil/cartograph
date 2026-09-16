---
name: refactor-safely
description: Plan a rename or structural change by previewing every site it would touch before editing anything. Use when renaming a symbol, removing dead code, or looking for refactoring candidates. Not for reviewing a diff that already exists (use review-changes) or for diagnosing a bug (use debug-issue).
---

## Refactor safely

Previews the reach of a change before you make it, so the edit is informed by
the whole call graph rather than by grep.

### `carto refactor` never edits your code

Every mode is a **preview**. There is no apply step and no `--write` flag:
Cartograph reports the sites, you make the edits with your normal tools. If a
plan says "then carto applies the rename", the plan is wrong.

### When to use this
- Renaming a function, class, or method across a codebase.
- Hunting dead code before a cleanup.
- "What would break if I changed this?"

### When NOT to use this
- Assessing a change that already exists → `review-changes`.
- Finding the cause of a bug → `debug-issue`.

### Steps

1. **Preview the rename.** Modes are `rename`, `dead_code`, `suggest`.
   ```
   carto refactor rename --old-name <old> --new-name <new> --format json
   ```
   Narrow with `--kind Function|Class` and `--path <pattern>` when the name
   is common.

2. **Cross-check the reach.** The rename preview and the graph should agree;
   where they disagree, trust neither and read the code.
   ```
   carto query references_to <symbol> --format json
   carto impact --files <file> --depth 2 --format json
   ```

3. **Check the tests** that will need updating in the same commit:
   ```
   carto query tests_for <symbol> --format json
   ```

4. **Make the edits yourself**, then keep the graph honest:
   ```
   carto update --base HEAD~1
   ```

5. **Verify** with `review-changes` against your own diff before committing.

### Finding candidates

- Dead code: `carto refactor dead_code --format json`, or
  `carto dead-code --json` for the standalone report.
- Suggestions: `carto refactor suggest --format json --max-tokens 3000` —
  it can return hundreds, so budget it.
- Oversized functions: `carto large-functions --min-lines 80 --format json`.

### Care

- **Dead code is a candidate list, not a delete list.** Reflection, dynamic
  dispatch, plugin entry points and public API all look dead to a static
  graph. Confirm each one before removing it.
- A stale graph makes every answer here wrong in the direction that costs you
  most — missing call sites. Run `carto status --format json` first and check
  `data.stale`.

### If it fails

Exit `2` means no graph or a stale one — run `error.remediation` or use the
`build-graph` skill, then retry.

### Report

List the sites the change touches, grouped by file, with anything the graph
could not resolve called out separately. Say plainly which edits you made and
which you left for a human.

### Reference

`carto capabilities --command refactor` for the full flag list.
