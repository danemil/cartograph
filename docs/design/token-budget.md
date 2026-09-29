---
tags: [design, contract, token-budget, engine]
created: 2026-09-16
status: implemented
---

# `--max-tokens` — the token budget

`--max-tokens` is accepted on every agent-facing command. This is what it
promises and how it keeps the promise.

What a response costs *before* any budget applies — compact rows, one-line
summaries — is in `compact-output.md`.

## The promise

> **Either the response fits the budget, or it says on its face that it does not.**

A response that quietly overruns is the failure mode a token budget exists to
prevent — the agent has already paid for the context by the time it finds out.
So the envelope carries `size.budget_tokens` (what was asked) and, when the
floor is still too large, `size.over_budget: true`.

## Truncation is semantic, never a byte cut

A byte cut hands the agent invalid JSON, which is strictly worse than a smaller
valid answer: it cannot be parsed, so it cannot even be partially recovered.
Nothing in `fit()` truncates a string or a structure mid-way. Whole units of
meaning are given up, cheapest loss first.

## The drop order

1. **Supporting context**, least valuable first. Two kinds live here — members
   of `data.facets`, and top-level lists in `data` that are not the answer.
   Contract amendment A8 says exactly one collection is the answer; every other
   list is supporting detail by definition, which is what makes this rule
   principled rather than a list of special cases.
2. **The tail of the answer collection.** It is ranked, so its tail is the least
   relevant part of the work queue. Found by binary search — about five
   re-measurements on a 25-item page.
3. **Stop.** `data.summary` is the floor and is never dropped. A response
   without it is not a cheaper answer, it is no answer.

Value ranking lives in `_CONTEXT_VALUE`. Anything unranked goes first: context
we cannot vouch for is the safest thing to lose, and that default means a facet
added later degrades gracefully instead of silently outranking the diff.

For `review-context` this yields exactly the intended order —
`source_snippets` → `edges` → `changed_nodes` → trim `items` — leaving the
cheap, high-value file lists standing.

## What the agent sees

| Signal | Meaning |
|---|---|
| `truncated_reason: "max_tokens"` | Narrow the query. Outranks `page_limit`: both may be true, but this is the advice that helps, and `page.has_more` still carries the rest. |
| `data.context_omitted` | `{keys, reason}` — which supporting context was withheld, and why. |
| `<facet>.omitted` | How many entries a facet withheld. Its `total` still reports the true count, so "48 edges exist, none shown" stays distinguishable from "no edges". |
| `size.over_budget` | Not even the floor fit. Narrow the query rather than assume you saw everything. |

## Measured

`review-context --limit 5` on this repo: **8,625 tokens → 1,154** at
`--max-tokens 1200`, keeping all five impacted nodes and the whole summary.

## Two traps worth remembering

**The size block is part of what it measures.** Writing a count into `size`
changes the count, so `_with_size` iterates to a fixed point. This stopped
mattering cosmetically and started mattering for correctness the moment `fit`
made a hard promise about `tokens_estimated`.

**Flags cost characters too.** `truncated`, `truncated_reason` and a
synthesised `page` block all add bytes. Setting them *after* fitting measures
an envelope that is not the one being emitted — which is exactly how a response
lands over budget with `over_budget` unset. They are installed before the
reduction runs, and withdrawn only if nothing was given up (withdrawal only
shrinks, so it cannot push a fitting response back over).

## Why `capabilities` trims rather than sheds

`capabilities` has no facets and no conventional `items`/`results`, so the
generic rule would have shed its whole `commands` list. An agent shown zero
commands concludes none exist — a far worse outcome than a short list. A
response with exactly one list has no ambiguity about which one is the answer,
so `_collection_key` treats it as such: the catalogue is trimmed and flagged,
and a `page` block is synthesised to report the true total.
