---
name: explore-codebase
description: Understand how an unfamiliar codebase is shaped — its architecture, clusters, execution flows, and where a concept lives. Use when onboarding, before planning a change, or when asked how something works. Not for reviewing a specific diff (use review-changes) or tracing a known bug (use debug-issue).
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
- You already know the symbol and want its callers → `carto query` directly.

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
