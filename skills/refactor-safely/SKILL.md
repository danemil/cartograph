---
name: refactor-safely
description: Who calls or uses a symbol, and what would break if it changed — callers, where it is used, blast radius, impact of a signature change, rename or removal — from the call graph, more completely than grep. Also dead code. Not for an existing diff (review-changes).
---

## Refactor safely

Answers "who calls this, and what breaks if I change it?" from the call graph,
and previews a rename before you make it. The graph resolves calls through
imports, aliases and methods, which a text search confuses with comments,
strings and same-named symbols in other files.

### When to use this
- "Who calls X?", "Where is X used?"
- "What would break if X's signature changed?", "What's the blast radius /
  impact of changing X?"
- Renaming a function, class or method; removing it; hunting dead code.

### When NOT to use this
- Assessing a change that already exists → `review-changes`.
- Finding the cause of a bug → `debug-issue`.

### `carto refactor` never edits your code

Every mode is a **preview**. There is no apply step and no `--write` flag:
Cartograph reports the sites, you make the edits with your normal tools.

### Who calls it, and what breaks

1. **Direct callers** — the code that breaks first when a signature changes:
   ```
   carto query callers_of <symbol> --limit 50 --format json
   ```
   A target is a name, or `path/to/file.py::Name` when the name is ambiguous
   (the response's `disambiguation` rows carry the names to pass back). If `truncated` is true, pass
   `page.next_cursor` back with `--cursor` until it is not.

2. **Other uses** — passed as a callback, imported, inherited from:
   ```
   carto query references_to <symbol> --format json
   ```

3. **Tests** that will need updating in the same commit:
   ```
   carto query tests_for <symbol> --format json
   ```

4. **The wider reach**, transitively, from the file that defines it:
   ```
   carto impact --files <file> --depth 2 --format json
   ```
   `impact` lists the 20 most affected items, direct dependents first. It
   never shortens the scope: `data.totals` counts everything, and
   `data.affected_files` names **every** affected file with how many of its
   items are direct. If `truncated` is true you have not seen every item —
   list every direct dependent (they rank first, so the limit is
   `data.totals.direct`), or run `data.see_all` for all of it:
   ```
   carto impact --files <file> --limit <totals.direct> --format json
   ```

Steps 1 and 4 answer most "what would break" questions; add 2 and 3 when the
change is a removal or the symbol is not only called.

`query`, `refactor` and `dead-code` carry `data.coverage`. When it says
code files are not covered, a caller may sit in one of them: say so in the
answer, and `grep` those files for the symbol before calling a list complete,
a rename finished or a symbol dead.

### Renaming

1. **Preview the rename.** Modes are `rename`, `dead_code`, `suggest`.
   ```
   carto refactor rename --old-name <old> --new-name <new> --format json
   ```
   Narrow with `--kind Function|Class` and `--path <pattern>` when the name
   is common. Cross-check it against steps 1–2 above; where they disagree,
   read the code.

2. **Make the edits yourself**, then keep the graph honest:
   ```
   carto update --base HEAD~1
   ```

3. **Verify** with `review-changes` against your own diff before committing.

### Finding candidates

- Dead code: `carto refactor dead_code --format json`, or
  `carto dead-code --json` for the standalone report.
- Suggestions: `carto refactor suggest --format json --max-tokens 3000` —
  it can return hundreds, so budget it.
- Oversized functions and methods: `carto large-functions --min-lines 80 --format json`
  (every one over 80 lines; `--limit N` alone gives the N largest; generated
  files left out; `--kind Class` widens). For the largest files, `wc -l` is
  cheaper.

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

`carto capabilities --command query` and `--command refactor` for the full
flag lists.
