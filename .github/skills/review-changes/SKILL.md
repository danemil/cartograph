---
name: review-changes
description: Risk-score a diff before it lands — working tree, a commit range, or a checked-out PR branch. Use before approving, merging, or writing review comments, to find what a change touches beyond the files it edits. Not for open-ended "how does this codebase work" questions (use explore-codebase) or for root-causing a reported bug (use debug-issue).
---

## Review changes

Risk-scores a diff using the knowledge graph instead of reading every touched
file cold. The graph narrows what you read; it does not replace reading it.

### When to use this
- Reviewing a PR, a branch before merge, or your own working tree.
- "Is this safe to merge?" / "What does this change touch?"

### When NOT to use this
- No diff yet, you are just understanding the code → `explore-codebase`.
- Chasing a bug with no known change → `debug-issue`.
- Planning a rename or structural change → `refactor-safely`.

### Scope

There is no `--pr` flag. A PR is reviewed by checking out its branch and
diffing against its base. Pick one scope and use it for every step below:

- working tree → omit `--base` (defaults to `HEAD~1`)
- a range → `--base <base-ref>`
- a checked-out PR → `--base origin/main` (or whatever it targets)

### Steps

1. **Triage cheaply.** One small call: risk, counts, test gaps, key entities.
   ```
   carto review-summary --base <ref> --format json
   ```
   If risk is low and there are no test gaps, say so and stop. Do not spend
   a full context pull on a two-line change.

2. **Pull the context** only if step 1 warrants it.
   ```
   carto review-context --base <ref> --limit 10 --format json --max-tokens 3000
   ```
   `data.items` are the impacted nodes — the review queue. `data.summary`
   carries risk and guidance. `data.facets` is bounded supporting context.

3. **Blast radius** for anything that looks load-bearing:
   ```
   carto impact --files <file> --depth 2 --format json
   ```
   Ranked, direct dependents first, 20 by default. `data.affected_files` is
   every affected file even when the item list is short; `data.totals` is
   exact. Read `truncated` before concluding anything is unaffected.

4. **Test coverage** for each high-risk symbol:
   ```
   carto query tests_for <symbol> --format json
   ```
   An empty result is a real answer: nothing tests it.

5. **Only now read the diff** — and only the parts steps 1–4 flagged.

### Budget

- Step 1 before step 2, always. `review-summary` is a fraction of the cost.
- Pass `--max-tokens` on anything that might be large. Truncation is
  semantic, so a truncated response is still valid and still useful; check
  `truncated_reason` and `size.over_budget`.
- Target: 4–6 calls before you start reading source.

### Reading the output

- `truncated_reason: "max_tokens"` → narrow the query.
  `"page_limit"` → there are more results than you asked for; `impact` names
  the command that lists them all in `data.see_all`.
- `data.context_omitted` names supporting context dropped for budget.
- A facet with `items: []` but a non-zero `total` was withheld, not empty.

### If it fails

Exit `2` means no graph or a stale one — run `error.remediation` (see the
`build-graph` skill), then retry. Exit `1` means the call was malformed.

### Report

Group by risk. Per finding: what changed, why it matters, whether tests cover
it, and one merge recommendation. Do not enumerate changed lines — that is
what the diff is for.

### Reference

`carto capabilities --format json`, or `--command <name>` for full flags.
