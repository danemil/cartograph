---
name: explore-codebase
description: How a repository is structured — its main components and how they connect — where a concept lives, and the largest functions by line count. Use when onboarding. Not for who calls a symbol or what a change breaks (refactor-safely).
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
- Largest *files* → `wc -l`, e.g. `git ls-files '*.ts' | xargs wc -l | sort -n | tail`.
  Largest *functions* are not a file question: the shell cannot see where a
  function ends, so use `carto large-functions` (step 5).
- Which files mention a string → `grep -rln` (or `rg -l`).
- Listing or counting files → `find`, `git ls-files`.

The graph earns its cost where it answers what the file system cannot:
callers and callees, impact, execution flows, function-level structure, and
architecture.

### Steps

1. **Start the overview with one call.** For "how is this repo structured /
   what are its main components and how do they connect":
   ```
   carto architecture --format json
   ```
   `layout` comes first: every file git lists, counted by top-level directory
   and kind (code, docs, config, other), and how many of them the graph
   parsed. A directory holding a large share of the repository also names
   what is inside it: `sub:` its second-level directories, then the
   components found in it — `skills:` (with how many), `server:` (server
   entry points), `app:` (dashboards, web apps), `tests:`, `hooks:`,
   `ci:`. Each of those is a component of the answer, under its own name.
   Then one row per code component (community): its name, size,
   language and the directories it lives in; one row per connected pair: edge
   count, edge kinds, and `high coupling` where it is.

   **The graph covers parsed code only.** When `layout.note` is present, most
   of the repository is not code and the communities describe only its code
   part. Then read the files the root row names under `read first:` (the
   README, AGENTS.md) and the top-level docs they point to, and describe the
   repository from its layout and those docs — the communities are one part
   of it, not its structure. When there is no note, the code is most of the
   repository and the overview can be answered from this call. Either way, do
   not follow it with `communities` or `flows` by reflex — every later call
   re-sends this one.

2. **Drill into one area** only when the question names it:
   ```
   carto community --name <name> --format json
   ```

3. **Execution paths**, only when asked how something runs end to end:
   ```
   carto flows --sort criticality --limit 10 --format json
   ```
   Then: `carto flow --id <id> --format json`.

4. **Locate a concept** when you do not know its symbol name:
   ```
   carto search "<what you are looking for>" --limit 10 --format json
   ```
   Check `search_mode`: `semantic` and `hybrid` mean embeddings participated;
   `keyword` means lexical matching only, so trust it less for near-misses
   and try the literal term.

5. **The largest functions**, or the rough edges when sizing up quality:
   ```
   carto large-functions --limit 10 --format json
   ```
   The N largest functions and methods, test functions included (their rows
   say `Test`), with no size threshold — "top 10" is `--limit 10`. Add
   `--min-lines 80` only for "every function over 80 lines". Generated and
   `.d.ts` files are left out (the summary says how many). `--kind` replaces
   the default and repeats: `--kind Function` for non-test code only, or
   `carto large-functions --kind Function --kind Class --include-generated`.
   For the largest *files*, use `wc -l` instead.

### Coverage: say what the answer did not see

`large-functions`, `search`, `query` and `dead-code` carry `data.coverage`,
e.g. `searched 70 of 73 code files; not covered: 2 no parser (.bat), 1
generated or vendored`. When it says anything is not covered, say so in the
answer — name the counts and kinds — rather than presenting the list as
complete; if the uncovered files could hold the answer (a `.bat` cannot hold
the largest Python function; a file `not in the graph` can), check them with
the shell or run `carto update` and ask again.

### Reading the rows

List results are one line per row: `301 lines | Function | Store.migrate |
src/store.ts:20` — the number the command ranks by, kind, name, `path:line`.
To pass a row back as a target, join path and name: `src/store.ts::Store.migrate`.
Add `--detail full` only when you need a field the row leaves out.

### Budget

- An overview of a code-heavy repository is one call: step 1. A mostly
  non-code one is step 1 plus the docs it names. Each later step is for a
  question the overview does not answer.
- `--detail-level standard` adds member samples and every cross-community
  edge — 8 to 22 times the size on a 991-file repository, rarely what an
  overview needs.

### If it fails

Exit `2` means there is no graph yet — run `error.remediation`, or use the
`build-graph` skill, then retry.

### Report

Lead with the shape: the major components, where each lives and what it is
for. Name every component the layout names — the parts inside a dominant
directory too (its skills, servers, apps, tests, hooks, CI), not just the
top-level directories. Where most of the repository is docs or templates,
those are major components too; say what they are from reading them, not
from their file counts.

Then say how the components connect: what calls, feeds, copies, runs or
serves what — e.g. a workflow runs the validators, ingestion writes the store
that a server or dashboard reads, a template is copied into new projects.
Take these from the README and docs you read, and from the code's own entry
points; communities and their coupling counts say only that code shares
edges, which is not how a reader's components connect. Name specific files
and symbols — a summary the reader cannot act on is not worth the tokens.

### Reference

`carto capabilities --format json` lists every command, including the ones
this skill does not use.
