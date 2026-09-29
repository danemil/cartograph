---
tags: [memory, design, cartograph]
updated: 2026-09-29
---

# Memory design

What Cartograph stores, what it spends to store it, and how an agent reads it
back — and why that differs from claude-mem, whose value it rebuilds.

## claude-mem, as the code has it

Read at commit `ade13f3` (2026-09-28). Where its docs disagree, the code is
quoted.

| | claude-mem | Source |
|---|---|---|
| Captured | Your prompts; **every** tool call's full input and output; each turn's last assistant message | `plugin/hooks/hooks.json`, `src/cli/handlers/observation.ts:18-31`, `summarize.ts:88-106` |
| Raw kept | Yes — full tool I/O in `tool_uses` | `src/services/sqlite/tool-uses.ts:173` |
| Condensed by | Haiku 4.5 via the Agent SDK, tools off — **one call per tool call** | `SettingsDefaultsManager.ts:193`, `ClaudeProvider.ts:778-842` |
| Summaries | **Per turn**, not per session (docs say per session) | `prompts.ts:340-347`, `plugin/modes/code.json` |
| Pushed to a new session | 50 observation **titles**, 10 turn summaries, the last in full; hard cap 10,000 characters | `SettingsDefaultsManager.ts:194,225`, `ContextBudget.ts:9` |
| Read back | Index → `get_observations(ids)` → raw tool I/O | `mcp-server.ts:445-481` |

Its **Stats line** (`TokenCalculator.ts`) is not a measured saving: "read" is a
chars/4 estimate of reading every listed observation in full (not what was
injected), "work" is the compression model's own spend — stamped on every
observation a call produced, so counted more than once
(`SessionStore.ts:3156-3177`) — and "savings" divides one by the other.

## What Cartograph does instead, and why

The constraint that decides it: **every summary spends the user's Copilot
quota**, where claude-mem's Haiku calls ride a Claude subscription. Condensing
every tool call would bill every file read.

| | Cartograph | Why |
|---|---|---|
| Model calls to write memory | **One per session**; at most 3 per catch-up run | Copilot quota |
| Model | `copilot -p --model auto` (override: `CARTO_SUMMARY_MODEL`) | Auto routes a short brief to a light model (measured: `gpt-6-luna`), is 10% cheaper on paid plans, and never picks a model an administrator blocked — a pinned name that policy blocks would make every summary silently structural |
| Summary input | Each prompt **and that turn's final assistant reply**, from the logs; reply clipped to 600 chars | Prompts alone say what was asked, not what was concluded |
| Summary fields | TITLE · WORKED ON · **DECIDED** (only what the person stated or accepted) · **PROPOSED** (what the assistant suggested, unconfirmed) · DEAD ENDS | A summary must never record a decision nobody made |
| Raw evidence kept | Prompts, verbatim | The graph answers code questions more currently than stored tool output would |
| Replies stored? | **No** — read from the logs at summary time only | A reply is not a prompt and must not be searchable as one |
| Pushed to a new session | **Nothing** | Every session would pay for it; Copilot Chat cannot take a plain-text context block anyway. The `recall-context` skill pulls when needed |
| Read back | `mem search` (snippets) → `mem show --id` (full text) | Same index-then-detail split, without the push |

Measured live on 2026-09-29: a session where the person stated the decision
came back `DECIDED: …, PROPOSED: none`; a session where the assistant proposed
a fix the person never accepted came back with the fix under `PROPOSED` and
only the person's own call under `DECIDED`. **Known limitation:** in that
second session an earlier, parked recommendation was left out of `PROPOSED`
altogether — omitted, not promoted.

## What memory cost

`carto mem status` (and the extension's status bar tooltip) show two lines,
only from what can be counted:

```
memory cost   1 summary call(s) to Copilot · 1 recall(s) served ≈ 260 tokens (chars/4)
vs raw logs   1 summarised session(s): summaries ≈ 73 tokens vs their prompts + replies ≈ 149 tokens (chars/4)
```

- **Summary calls** — counted when made, failed ones included (they spent
  quota too).
- **Recalls served** — every `mem search` / `mem show` response, sized at
  chars/4 when it is built.
- **vs raw logs** — the stored summaries against the full prompts and replies
  of the sessions they stand for.

Never injected into a session: a line on every session to report on tokens
would itself cost tokens on every session. And where memory is not paying for
itself, these lines say so.

## Not done, deliberately or yet

- **Per-turn summaries** — finer than per session, and a model call per turn.
- **Semantic search** — everything is `search_mode: keyword`.
- **Summarising through VS Code's language-model API** rather than the Copilot
  CLI, which would work on a machine with no `copilot` binary.
