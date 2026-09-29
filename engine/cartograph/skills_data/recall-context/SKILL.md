---
name: recall-context
description: Search what earlier sessions recorded about this repository, and record decisions worth keeping. Use before re-deriving something that was probably already worked out, and after settling a decision or hitting a dead end. Not for questions the code itself answers — use explore-codebase or debug-issue for those.
---

## Recall what was learned before

Sessions end; what they worked out does not have to. This skill reads that
record and adds to it.

It answers questions about **this project's history** — what was decided, what
was tried, what turned out to be a dead end. It does not answer questions about
the code, which the graph answers better and always knows more currently.

### When to use this
- You are about to reason out something that smells previously settled:
  "why is it done this way", "did we try X", "what was wrong with Y".
- You just made a non-obvious decision, or ruled something out after real work.

### When NOT to use this
- "What calls this function?" → `explore-codebase` or `carto query`.
- "Why is this failing?" → `debug-issue`. Memory holds opinions, not facts about
  current code; the code may have moved since anyone wrote about it.
- A fresh checkout nobody has worked in. There is nothing to recall.

### Steps

1. **Is there anything to recall?** Cheap, and it saves a pointless search.
   ```
   carto mem status --format json
   ```
   Exit `2` means no store yet — nothing has been recorded here. Say so and
   move on; do not treat it as a failure.

2. **Search before deriving.**
   ```
   carto mem search --query "<the thing you were about to work out>" --format json
   ```
   Narrow with `--session` to stay inside one conversation, or `--date-start`
   when recency matters more than relevance.

   Earlier sessions are recorded for you: every prompt verbatim, and one
   summary per session with WORKED ON / DECIDED / PROPOSED / DEAD ENDS lines.
   DECIDED holds only what a person stated or accepted; PROPOSED is what an
   assistant suggested that nobody confirmed — do not treat it as settled.
   To read only the summaries, which is usually what "did we already try
   this" wants:
   ```
   carto mem search --query "<topic>" --doc-type sessions --format json
   ```
   `summary_source: host-agent` means a model wrote it from the session;
   `structural` means it is only a list of what was asked, not a synthesis.

   Search returns a short snippet per row. Read the whole of only the rows
   that matter, by id:
   ```
   carto mem show --id <id> --format json
   ```

3. **Read `search_mode` before you trust a near miss.** `semantic` and `hybrid`
   mean embeddings participated. `keyword` means lexical matching only — so a
   query phrased differently from what was recorded will miss, and you should
   retry with the words someone would actually have written.

4. **Record what is worth keeping**, once you have settled something:
   ```
   carto mem add --title "<the decision, in one line>" --kind decision --format json
   ```
   Use `--body` for the reasoning, and `--file` for the paths it concerns.

### What is worth recording

The test is whether a future session would waste time without it.

- **Decisions and their reasons**, especially where an obvious alternative was
  rejected. The reason is the valuable half; the decision alone is visible in
  the code.
- **Dead ends.** "We tried X, it failed because Y" is the single highest-value
  thing here, because nothing else in the repository records it. The code shows
  what was built, never what was abandoned.
- **Gotchas** that cost real time and are not obvious from reading.

Not worth recording: anything the code, the graph, or `git log` already says.
A memory that restates the diff is weight every future search pays to skip.

### Budget

- Step 1 before step 2. `mem status` is a fraction of a search.
- One search is usually enough. If two differently-phrased queries both return
  nothing, nothing was recorded — stop looking and work it out.
- `--max-tokens` bounds any response; truncation is semantic, so the JSON stays
  valid and `truncated_reason` tells you whether to narrow.

### If it fails

Exit `2` means no store exists yet; `error.remediation` names the command that
creates one. Exit `1` means the call was malformed — check
`carto capabilities --command "mem search"`.

### Reference

`carto capabilities --format json` lists every command.
