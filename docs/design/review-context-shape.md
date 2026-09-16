---
tags: [design, review-context, engine, advisory]
created: 2026-09-16
advisor: codex
status: accepted-with-one-amendment
---

# `carto review-context` — the output shape

Advice from an independent Codex review, plus one amendment. Findings that
prompted it: [[review-context-findings]].

*Codex could not write this file itself — the path was outside its sandbox root,
which was the notes vault rather than this repo. Its recommendations are
transcribed faithfully below and marked where I departed from them.*

## The tension

`standard` returns **five** list-shaped collections; contract amendment A8
permits exactly **one** pageable collection per envelope, because a cursor over
two collections cannot be interpreted unambiguously.

## 1 — Page `impacted_nodes`, and only that

**ACCEPTED.** It is the actual review work queue: the thing an agent walks
through and may need to continue. `changed_files` is bounded by the diff and
usually small; `edges` are supporting detail, not a queue.

## 2 — The other four become bounded facets

**ACCEPTED, WITH ONE AMENDMENT.**

`changed_files`, `impacted_files` and `changed_nodes` become bounded entries in
`facets`, each keeping its existing `_total` count as the honesty signal. They
are never paged.

`edges` are not paged either. A small bounded set is inlined per impacted node.

**Amendment:** Codex proposed a new `carto impact-graph` command for graph
depth. **Rejected — `carto impact` already exists** and is exactly "the blast
radius of a change". Adding a second command for the same concept splits the
surface for no gain. Depth goes to `carto impact`, which already accepts
`--depth`. One fewer command to document, teach in a skill, and keep conformant.

## 3 — `minimal` becomes its own command

**ACCEPTED.** `detail_level=minimal` returns a genuinely different response —
counts, risk and suggestions, no lists at all. Returning two incompatible shapes
under one `tool` value weakens the typed-`data` promise the contract makes.

It becomes **`carto review-summary`**, with `tool: "review-summary"`. Cheap for
an agent to call first and decide whether it needs the full context.

## 4 — The shape

**ACCEPTED.**

```json
{
  "schema": 1,
  "ok": true,
  "tool": "review-context",
  "data": {
    "items": [
      {
        "id": "code_review_graph.cli:main",
        "title": "main",
        "kind": "function",
        "location": {"file": "engine/code_review_graph/cli.py", "line": 1462},
        "score": 0.87,
        "snippet": "def main() -> None:",
        "metadata": {
          "reason": "calls changed symbol",
          "edges": [
            {"kind": "CALLS", "to": "code_review_graph.envelope:emit"}
          ]
        }
      }
    ],
    "summary": {
      "risk": "medium",
      "changed_file_count": 3,
      "impacted_file_count": 1,
      "test_gaps": 2,
      "review_guidance": "…",
      "context_savings": {
        "estimated": true,
        "saved_tokens": 24863,
        "saved_percent": 99,
        "ratio": "12.4x"
      }
    },
    "facets": {
      "changed_files": {"items": ["…"], "total": 3},
      "impacted_files": {"items": ["…"], "total": 1},
      "changed_nodes": {"items": ["…"], "total": 10},
      "edges_total": 48
    }
  },
  "truncated": false,
  "size": {"chars": 0, "tokens_estimated": 0, "estimator": "chars/4"},
  "provenance": {"graph_sha": "…", "built_at": "…"},
  "page": {"limit": 25, "has_more": true, "next_cursor": null, "result_count": 25}
}
```

`page` describes `items` only, so `page.collection` is omitted (it is only
needed when the pageable collection is not `items`).

## 5 — `next_tool_suggestions` goes

**ACCEPTED.** Workflow choice belongs to the skill, not the tool — and the
current values name MCP tools that will not exist. If suggestions survive
anywhere it is in `review-summary`, as concrete `carto` commands.

## 6 — Savings must not vanish silently

**Added here, not from the advice.** Per finding 1, `context_savings` disappears
entirely when the token baseline cannot be computed. The CLI emits
`summary.context_savings: null` with a `context_savings_unavailable` reason
rather than omitting the key, so an agent can tell "no saving" from "not
measured".

`saved_percent` is capped at 99 — a response is never free — and a `ratio`
field is added, since "12.4x" is the honest form of the claim.

## Risks worth watching

- **Scoring `items`.** Paging is only useful if order is meaningful. If
  `impacted_nodes` has no real ranking, page 2 is arbitrary and semantic
  truncation degrades to "drop whatever was last".
- **`facets` becoming a junk drawer.** It is for bounded context, not for
  everything that did not fit.
- **Two commands drifting.** `review-summary` and `review-context` must agree on
  risk and counts; they should share one computation, not two.
