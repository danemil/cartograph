---
name: explore-codebase
description: How a repository is structured — its main components and how they connect — and where a concept lives. Use when onboarding or asked how the repo works. Not for who calls a symbol or what a change breaks (refactor-safely).
---

## Explore a codebase

Answers "how is this shaped?" from the graph, so you can skip the phase where
you open twenty files to find the three that matter.

### When to use this
- Onboarding to an unfamiliar repo, or asked to explain how it works.
- Planning a change and you do not yet know where it belongs.
- Looking for where a concept lives when you do not know the symbol name.

### When NOT to use this
- You have a diff to assess → `review-changes`.
- You have a symptom to trace → `debug-issue`.
- Who calls a symbol, or what a change to it would break → `refactor-safely`.

### When NOT to use carto at all
File-level questions are cheaper with the shell, and just as exact:
- Largest files, line counts → `wc -l`, e.g. `git ls-files '*.ts' | xargs wc -l | sort -n | tail`.
- Which files mention a string → `grep -rln` (or `rg -l`).
- Listing or counting files → `find`, `git ls-files`.

The graph earns its cost where it answers what the file system cannot:
callers and callees, impact, execution flows, function-level structure, and
architecture.

### Steps

1. **Start at the top.** One call, bounded, gives the shape of the whole repo.
   ```
   carto architecture --detail-level standard --format json --max-tokens 3000
   ```
   Use `--detail-level minimal` first on a very large repo.

2. **Find the clusters** — which parts belong together.
   ```
   carto communities --sort size --min-size 5 --format json
   ```
   Then read one in detail: `carto community --name <name> --format json`.

3. **Follow the important paths.** Flows are execution paths, not files —
   this is the part a directory listing cannot tell you.
   ```
   carto flows --sort criticality --limit 10 --format json
   ```
   Then: `carto flow --name <name> --format json`.

4. **Locate a concept** when you do not know its symbol name:
   ```
   carto search "<what you are looking for>" --limit 10 --format json
   ```
   Check `search_mode`: `semantic` and `hybrid` mean embeddings participated;
   `keyword` means lexical matching only, so trust it less for near-misses
   and try the literal term.

5. **Spot the rough edges**, when sizing up quality or planning work:
   ```
   carto large-functions --min-lines 80 --limit 20 --format json
   ```
   Functions and methods only, generated and `.d.ts` files left out (the
   summary says how many). `--kind` widens and repeats, e.g.
   `carto large-functions --kind Function --kind Class --include-generated`.
   For the largest *files*, use `wc -l` instead.

### Reading the rows

List results are one line per row: `301 lines | Function | Store.migrate |
src/store.ts:20` — the number the command ranks by, kind, name, `path:line`.
To pass a row back as a target, join path and name: `src/store.ts::Store.migrate`.
Add `--detail full` only when you need a field the row leaves out.

### Budget

- Steps 1–3 answer most questions in three calls. Do not run all five by
  reflex — pick what the question needs.
- Pass `--max-tokens` on `architecture`; it grows with the repo.

### If it fails

Exit `2` means there is no graph yet — run `error.remediation`, or use the
`build-graph` skill, then retry.

### Report

Lead with the shape: the major clusters and what each is for. Then the flows
that matter. Name specific files and symbols — a summary the reader cannot
act on is not worth the tokens.

### Reference

`carto capabilities --format json` lists every command, including the ones
this skill does not use.
