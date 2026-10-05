---
name: refactor-safely
description: Who calls or uses a symbol, and what would break if it changed — callers, where it is used, blast radius, impact of a signature change, rename or removal — from the call graph, with less noise than grep. Also dead code. Not for an existing diff (review-changes).
---

## Refactor safely

Answers "who calls this, and what breaks if I change it?" from the call graph,
and previews a rename before you make it. The graph resolves calls through
imports, aliases and methods, which a text search confuses with comments,
strings and same-named symbols in other files. It does not type-check, so a
caller it could not pin to this exact symbol ends with how it was found: `unresolved`
(by name), `ambiguous` (one of several candidates) or `via_supertype` (a call
through the interface or base class it overrides).

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

For one symbol — "who calls X, and what would break if its signature
changed?" — two calls answer it. Answer from them:

1. **Direct callers** — every call site a signature change can reach:
   ```
   carto query callers_of <symbol> --limit 50 --format json
   ```
   Then read the call lines before saying what breaks, and say it per kind
   of change: calls that pass arguments by position break when a required
   parameter is added, removed or reordered — not when one is renamed or
   made optional. Calls that pass by keyword break on a rename. A new
   optional parameter at the end breaks none of them where the language has
   optional parameters (Python, JS/TS, C#); Java and Go have none, so there
   any new parameter is a required one. "Any signature change
   breaks every caller" is wrong more often than right.
   A reorder often raises no error at the call: the values land in the wrong
   parameters and the failure shows up later, or never. Say so rather than
   promising a TypeError.
   A target is a name, or `path/to/file.py::Name` when the name is ambiguous
   (the response's `disambiguation` rows carry the names to pass back). If `page.has_more` is
   true, pass `page.next_cursor` back with `--cursor` until it is not.

2. **Tests** that call it or cover it, to update in the same commit:
   ```
   carto query tests_for <symbol> --format json
   ```
   A row ending `via <helper>` reaches the symbol through a fixture or helper
   in the test file: it breaks if the helper's call breaks.

3. **Other uses** — passed as a callback, imported, inherited from — when the
   change is a rename or a removal, which these break too:
   ```
   carto query references_to <symbol> --format json
   ```

Run `impact` only when asked what *else* could be affected, beyond the
callers:

4. **The wider reach**, transitively, from the file that defines it:
   ```
   carto impact --files <file> --depth 2 --format json
   ```
   Read it as file-level reach, not as call sites of your symbol:
   - It starts from **every** symbol in the file. Each row says how it
     depends on the change: `direct | calls add_node | …` is a caller of
     `add_node`; `direct | calls get_node | …` calls another function in the
     same file and is not touched by a change to `add_node`.
   - `direct | imports graph_store.py | …` only imports it. An import is not
     broken by a signature change — only a call is — though a rename or
     removal breaks it. The summary counts them apart (`9 direct: 4 call,
     5 import only`), and `affected_files` says `only imports` per file.
   - `transitive` rows are callers of callers (`calls ingest`): affected only
     if the direct caller's own behaviour or signature changes too.

   `impact` lists the 20 most affected items, direct dependents first. It
   never shortens the scope: `data.totals` counts everything, and
   `data.affected_files` names **every** affected file with how many of its
   items are direct. If `truncated` is true you have not seen every item —
   list every direct dependent (they rank first, so the limit is
   `data.totals.direct`), or run `data.see_all` for all of it:
   ```
   carto impact --files <file> --limit <totals.direct> --format json
   ```
   `impact` is its own command, not a `query` pattern.

`query`, `refactor` and `dead-code` carry `data.coverage`. When it says
code files are not covered, a caller may sit in one of them: say so in the
answer. Either way, `grep` for the symbol before calling a list complete,
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
