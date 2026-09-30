---
tags: [memory, design, cartograph]
updated: 2026-09-30
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
| …where `copilot` is not on PATH | The VS Code extension, through `vscode.lm`: Copilot's model with id `auto` (setting `cartograph.summaryModel`), labelled `platform_source: vscode-lm:<model-id>` | Same reasons as the CLI's `auto`. Copilot Chat registers Auto with `vscode.lm` as vendor `copilot`, id `auto` (`microsoft/vscode` `extensions/copilot/.../languageModelAccess.ts`, commit `3ba86b4`). The engine hands off (`mem sync --hand-off`), gives the extension the same brief (`mem summarise --brief-only`) and stores its answer through the host-answer path (`--answer-file`), so the one-summary rule and the cost counter are unchanged |
| …when neither can | Structural: the prompts in order, `summary_source: structural` | Consent refused, blocked by quota or policy, model not offered, timeout: each lands here, and says so |
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

## Meaning-based search (decision 2)

`mem search` runs FTS5 and a vector search side by side and merges them by
rank (`search_mode: hybrid`), so a memory is found when the question uses
different words from the record. A query with no searchable words at all runs
on vectors alone (`semantic`). Where the model is not available the answer is
`keyword`, and `data.semantic_unavailable` says why — the model directory is
not set, sqlite-vec cannot load, nothing is embedded yet. `mem status` reports
`semantic_search` and `embedded_observations`.

| | Choice | Why |
|---|---|---|
| Model | all-MiniLM-L6-v2, the upstream repository's own int8 ONNX export (23 MB), Apache-2.0, pinned revision + sha256 | Small, English, permissive; int8 held quality — cosine ≥ 0.993 against the fp32 model on the same sentences |
| Runtime | ONNX Runtime (CPU) + Hugging Face `tokenizers` + NumPy | PyTorch/sentence-transformers is an order of magnitude larger. Measured unpacked, macOS arm64: ORT's Python module 41 MB (its separate 33 MB C library pruned — nothing links it), tokenizers 9 MB, NumPy 7 MB frozen |
| Where the model is | `CARTO_EMBEDDING_MODEL_DIR`, set by the launchers and the extension to `<payload>/model`, as the grammar variable is | One mechanism for everything the payload carries |
| Vector index | sqlite-vec `vec0`, cosine distance, in `memory.db` | Decided earlier; unchanged. Rows below cosine 0.25 are dropped, so an unrelated question is an honest empty answer rather than five nearest strangers. Measured on memory-shaped text: unrelated questions scored at most 0.17, reworded ones sharing no keyword 0.32–0.42 |
| Graph `search` | Same provider (`get_provider` resolves the bundled model first), but only after `carto embed` | The graph's index is built on request, not on every `build`; the wiring cost nothing |

### Where embedding happens, and why

**Never in the prompt-capture hook.** It runs inside the host's turn at about
1 ms; loading the model would add roughly 0.1 s and 100 MB to every prompt.
Capture writes the row with `embed=False`, and a test asserts, in a fresh
interpreter, that capture imports none of onnxruntime, tokenizers, numpy or
sqlite-vec.

Vectors are written by whichever comes next, through one method
(`MemoryStore.embed_missing`):

- **`mem sync`** — unbounded. It runs off the host's turn (the extension's
  10-minute timer, after chat writes, a person), so it is where hook-captured
  prompts get their vectors and an older store is backfilled in full.
- **`mem add`, summaries** — the row itself plus up to 256 older rows missing one.
- **`mem search`** — up to 256 rows missing a vector before it searches, newest
  first. A store from before this release is therefore backfilled by being
  used; nobody has to know about a rebuild step. The cap keeps one search's
  detour under about two seconds.

An index written by a different model is rebuilt, not refused and not mixed:
vectors are derived from observations that are all still there.

### Measured

darwin-arm64 (Apple Silicon), frozen build, 2026-09-30. Search figures are the
median of 5 fresh `carto` processes — every invocation is a new process — with
the OS file cache warm; a disk-cold first run was not measured.

| | Before (v0.7.0) | After |
|---|---|---|
| `.vsix` | 32.8 MB | 62.1 MB (+29.3) |
| Payload, unpacked | 152 MB | 214 MB (+62; model 23) |
| `mem search`, keyword (model not set) | 0.16 s, 60 MB peak RSS | 0.19 s, 67 MB |
| `mem search`, hybrid | — | **0.27 s, 163 MB** |
| Embedding, per row (~150-word memory) | — | 6.4 ms |

The model's load is about 0.08 s per invocation, not the 0.5–1 s estimated
when the decision was taken, so no mitigation was needed. (The 0.5–1 s figure
holds for the very first import after boot of an unfrozen venv: 1.5 s was
measured there, and 0.1 s on every run after it.) Keyword search got 0.03 s
slower; the new payload was frozen from a different interpreter (uv's CPython
3.12 instead of python.org's 3.13, see packaging.md), and the keyword path
imports none of the embedding stack.

Commands:

```bash
python3 scripts/build-payload.py --target darwin-arm64 && ./scripts/build-vsix.sh
python3 scripts/ci_smoke.py darwin-arm64   # prints the table's "After" rows
```

linux-arm64, `docker/acceptance.sh linux/arm64` (native on this Mac, installed
with `install.sh` into a container with `--network none` and no Python):
payload 142 → 243 MB unpacked, and hybrid search finds the reworded memory.
Linux costs more than macOS because NumPy's wheel vendors OpenBLAS (27 MB);
PyInstaller also copied it a second time, which the build now removes.

The same script runs in every platform's CI job and prints these rows to the
job summary, so linux-x64 and win32-x64 have their own numbers. None have been
read yet.

## Not done, deliberately or yet

- **Per-turn summaries** — finer than per session, and a model call per turn.
- **A second, larger model**, or one for code rather than prose. The graph's
  own search would benefit most; nothing measured says the memory needs it.
- **Which model Auto chose.** Through `vscode.lm` the stored label is
  `vscode-lm:auto`; the API does not say which model Auto routed the request
  to. The CLI path records `copilot-cli` and no model at all.
- **Summaries while VS Code is closed.** The `vscode.lm` path runs only in an
  open window; sessions wait (unwritten) until one opens, or until a Copilot
  CLI hook or `mem sync --summarise` without `--hand-off` writes them.
- **`cartograph.summaryHost: vscode` does not reach the hooks.** Where
  `copilot` is installed, a CLI `SessionEnd` or a Chat `SessionStart` still
  summarises through it; the setting governs the summaries the extension
  runs.
