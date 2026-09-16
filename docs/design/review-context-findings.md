---
tags: [design, review-context, engine]
created: 2026-09-16
---

# `review-context` — findings before implementation

Measured against a real graph (270 files, 5785 nodes) built over `engine/`.

## The actual `standard` shape

```
status, summary, context: {
  changed_files[], changed_files_total,
  impacted_files[], impacted_files_total,
  graph: {
    changed_nodes[], changed_nodes_total,
    impacted_nodes[], impacted_nodes_total,
    edges[], edges_total
  },
  truncated, source_snippets{}, review_guidance
}
```

`minimal` returns a **different shape entirely**: `status, summary, risk,
changed_file_count, impacted_file_count, key_entities, test_gaps,
next_tool_suggestions` — no lists at all.

**Five list-shaped collections in `standard`** against contract amendment A8,
which permits exactly one pageable collection per envelope. That is the central
design question, and it is out for advice.

## Finding 1 — the savings block vanishes silently when paths do not resolve

`context_savings` is the 71x headline. It is attached unconditionally on both
paths (`review.py:208`, `review.py:310`), but `estimate_context_savings` returns
`None` when `baseline <= 0` (`context_savings.py:73`), and
`attach_context_savings` then simply does not set the key.

`baseline` comes from `estimate_file_tokens(root, changed_files)`. Git reports
changed paths relative to the **repo root**, so if `root` is a subdirectory the
paths do not resolve, every file contributes 0, and the metric disappears with
**no signal at all**:

```
repo_root='/Users/emidan/work/cartograph'  -> {'estimated': True, 'saved_tokens': 24863, 'saved_percent': 100}
repo_root='engine'                         -> None
```

This is the project's headline number silently becoming absent. Under the
contract, degradation must be visible: the CLI should either report
`context_savings: null` with a reason, or treat an unresolvable baseline as a
precondition problem — not omit the field and look successful.

*Note: this is also how I initially mis-diagnosed it as "never emitted" — the
silent absence is genuinely misleading, which rather makes the point.*

## Finding 2 — `saved_percent: 100` needs a sanity floor

`percent = round((saved / baseline) * 100)` reaches **100** whenever the
returned payload is ≲0.5% of the baseline — about 124 tokens against a 24,863
baseline. A compact `minimal` response clears that easily.

"100% saved" is not true — the response is not free. It reads as a bug to
anyone who looks, and it is the number the project is marketed on. A cap at 99,
or reporting the ratio (`71x`) rather than a percentage, would be more honest.
Upstream behaviour; recorded rather than fixed here.

## Finding 3 — `next_tool_suggestions` names tools that will not exist

The `minimal` path returns `["detect_changes", "get_affected_flows",
"get_impact_radius"]` — MCP tool names. Under Cartograph these are `carto`
commands, or the field goes away and suggestion becomes the skill's job.
Out for advice.
