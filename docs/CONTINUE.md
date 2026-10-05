---
tags: [handoff, cartograph]
updated: 2026-10-05
next-task: Linux .vsix acceptance on the user's machine, then transcript-aware summaries
---

# CONTINUE HERE

Resumption point for Cartograph. Read this first, verify state with the command
below, then pick up **Next task**.

## Verify state in one command

```bash
cd /Users/emidan/work/cartograph
./scripts/verify.sh
# expect: 399 envelope checks, 276 skills checks, 13/13 files, "all green"
```

If that passes, everything described here is true. If it does not, trust the
suite over this document.

## Session handoff

`docs/handoff-2026-09-25.md` carries what this file deliberately does not: the
state of the last conversation, the working conventions that were argued into
place, and how this user wants to be worked with. It is a dated snapshot — this
file wins on anything about what is built or what is next.

## What Cartograph is

Two MCP-dependent agent tools re-engineered into one MCP-free toolset, because
MCP servers are banned in the target organisation. Capability is exposed as a
**CLI + skills + hooks** instead. Targets GitHub Copilot CLI and Copilot Chat,
and nothing else. Acceptance test: a fresh machine with only VS Code (Copilot ships
built into VS Code 1.135.0), installing from a private repo, default-deny egress.

- Code: `/Users/emidan/work/cartograph` (private monorepo, Apache-2.0)
- Design record: `/Users/emidan/work/NOTES/20 Projects/FRQ/MCPs/` — wayfinder
  map, 17 resolved tickets, the capability-contract ADR + 9 amendments, and a
  reconciliation that wins where parallel resolutions disagree.

## Done so far (13 code commits)

| Commit | What |
|---|---|
| `e6287ed` | Scaffold: LICENSE, NOTICE, PROVENANCE, envelope schema, skeleton |
| `6d3ed84` | Vendored engine; `carto status`; the envelope; conformance suite |
| `e7f00cd` | All 16 query patterns (was 8); envelope on 10 graph commands |
| `64a9431` | `carto capabilities` — catalogue generated from the argparse parser |
| `478d3b8` | `carto review-context` + `carto review-summary` |
| `9c4c3e8` | `--max-tokens` enforced with semantic truncation |
| `38d5133` | Budget conformance cases pinned to explicit files |
| `e837aa0` | The five-skill pack, verified against the CLI |
| `84f6d4a` | Rename `code_review_graph` -> `cartograph` |
| `31bea8d` | `install` stops shipping MCP; the pack is what gets installed |
| `2eb682b` | Absolute paths no longer leak into review-context |
| `3fd38dc` | Triage of the 56 inherited failures + the real bug behind them |
| `82feb4b` | `carto hook` + cross-platform detached launcher |

The contract is honoured, the capability is reachable by an agent, and it now
fires on its own.

## Scope, restated by the user on 2026-09-29

**The only AI tool allowed in the target environment is GitHub Copilot — Chat
and CLI, inside VS Code.** No MCP, no Claude Code. Decisions 4 and 4b below
took the rest out of the code as well: `copilot` is the only install platform
and the only summariser host.

## Done on 2026-09-29

- `carto mem summarise` committed (`cb78033`): one `sessions` row per session,
  `host-agent` or `structural`, never mislabelled.
- **Frozen-binary re-entry fixed** (`41a83d4`). Detached work was launched as
  `carto -m cartograph …`; the PyInstaller binary has no `-m`, so every
  file-edit refresh in the v0.1.0 `.vsix` was a silent no-op.
- **Copilot hooks.** `.github/hooks/cartograph.json`, one file both hosts read;
  host named from `COPILOT_CLI`; duplicate-prompt refusal; summaries of
  sessions that never ended, run at the next `SessionStart`; graph refresh on
  `Stop`; the launcher directory appended to PATH inside every hook line,
  because Chat hooks do not see a terminal's PATH. The evidence, and what is
  still unverified, is in **`docs/copilot-hooks.md`** — read it before touching
  hooks.
- Proven live on macOS against Copilot CLI 1.0.82 and VS Code 1.139.1, both
  host-agent summary paths included. Nothing proven on Linux or Windows yet.

## Done later on 2026-09-29 — v0.3.0, the hooks-blocked fallback

The first Linux run found `chat.useHooks` **disabled by organisation policy**:
Chat hooks never ran. `carto mem sync` now imports prompts from the logs that
exist regardless — VS Code's `chatSessions/`, Copilot's `transcripts/`, and the
CLI's `~/.copilot/session-state/` — and records whether capture came via
hooks or logs (`mem status`: `capture_copilot_chat`, `capture_copilot_cli`).
The extension runs it on activation, after chat writes, and every 10 minutes
(`cartograph.readCopilotLogs`, on by default — the user's decision), and says
once when hooks are off. `build-vsix.sh` now refuses a payload older than the
engine source; a local build had silently shipped a 12-day-old engine.
Evidence in `docs/copilot-hooks.md`; runbook in `docs/verify-memory.md`.

## Done later still — v0.4.0, Cartograph Local

The user's target is VM / Remote SSH / Dev Containers, not local windows.
Measured: over Remote SSH VS Code keeps `chatSessions/` on the local (Windows)
machine; the VM has none. `companion/` is a UI-side extension that copies each
changed chat file to the remote via `cartograph.receiveChatSession`; the main
extension writes it to `.cartograph/chatSessions/` and `mem sync` reads it.
The main `.vsix` embeds the companion and offers to install it. Unit-tested
and built; **never run in a real remote window** — Dev Containers could not run
x64 on this Mac. The README now opens with the constraints table.

## Done — v0.5.0, memory design after the claude-mem comparison

`docs/memory-design.md` holds the comparison (claude-mem `ade13f3`, cited) and
the choices: one summary per session via `copilot -p --model auto`; the brief
carries each turn's final reply from the logs (never stored); DECIDED vs
PROPOSED; `mem show --id`; a counted cost line in `mem status` and the
tooltip, never injected. Both DECIDED/PROPOSED cases proven with live calls.

## Decided by the user on 2026-09-29 — build these next

| # | Decision | Choice | Notes |
|---|---|---|---|
| 1 | Summaries where `copilot` is not on PATH | **VS Code language-model API as fallback** | Order: `copilot -p --model auto`, then the extension via `vscode.lm`, then structural. One consent prompt; only while VS Code is open; subject to model policy |
| 2 | Meaning-based memory search | **Bundle a small embedding model now** | Quantized MiniLM-class ONNX model + runtime, ~+40–60 MB per `.vsix`, CPU only, no network, no Copilot quota. Embed during `mem sync`, never in the capture hook. Expect ~0.5–1 s model load per search — measure on the Linux VM |
| 3 | Files Cartograph writes into a repository | **Exclude locally** | Add `.github/hooks/cartograph.json` and the skills directories to `.git/info/exclude`; never touch the tracked `.gitignore` for them |
| 4 | Claude Code and other non-Copilot tools | **Remove all non-Copilot support** | Summariser host, `.claude/settings.json` hooks, and the inherited install paths for other tools (Cursor, Codex, Gemini, CodeBuddy, Qoder, OpenCode, …) and their tests. |
| 4b | The dormant MCP server (`carto serve`, `carto mcp`, `--with-mcp`) | **Remove it** | MCP is banned in the target environment. Drops `fastmcp` and the 7 test modules that import it |

### The plan, as it now stands

| Step | What | State |
|---|---|---|
| 1 | Removals — decisions 4 and 4b | **done** (`9a1ad15`, `353e57b`) |
| 1b | Narrow-lookup fix — `large-functions` answers with functions, compact rows, one-line summaries, skills say when `wc`/`find`/`grep` are cheaper | **done** (below) |
| 1c | `impact` default: top 20, ranked direct-first, every file and exact totals always present | **done** (below) |
| 2 | Exclude repo files locally — decision 3 | **done** (below) |
| 3 | VS Code language models as the summary fallback — decision 1 | **done** (below); needs a live window test |
| 4 | Bundle the embedding model — decision 2 | **done** (below); linux-x64 / win32-x64 proven only once CI runs |

## Done — decisions 4 and 4b

- **4b (`9a1ad15`).** `carto serve`, `carto mcp`, `--with-mcp`, `main.py`,
  `prompts.py`, `http_origin_guard.py`, the `mcp`/`fastmcp` dependencies, and
  what only the server used: the tool wrappers no CLI command calls,
  `analysis.py`, the packaged LLM reference, `apply_refactor` and the
  in-process preview store it needed. Of the 7 fastmcp test modules, 3 were
  MCP-only and deleted; 4 also covered CLI code and were kept with the MCP
  parts cut (`test_token_budget` now budgets the tool functions the CLI calls).
- **4 (`353e57b`).** Copilot is the only host. `carto install` writes
  `.github/skills/`, `.github/hooks/cartograph.json` and
  `.github/instructions/cartograph.instructions.md`, nothing else;
  `--platform` accepts `copilot` only. The summariser calls only `copilot`.
  `.claude/skills` and `.agents/skills` are gone because both Copilot hosts
  read `.github/skills` (`copilot skill --help`, CLI 1.0.82; VS Code's agent
  skills docs). `carto enrich` (a Claude Code `PreToolUse` helper) is gone.
  `carto uninstall` removes the Copilot files and no longer edits other hosts'
  configs. The pre-commit hook written by earlier `install.sh`/`install.ps1`
  runs (`--platform claude`) is left for the person to delete. Their
  `.claude/settings.json` hooks are now removed by `install` and `uninstall`
  (`legacy_hooks.py`): only commands identical to one a release wrote
  (`legacy_hooks.json`, 12, generated from each revision's own generator), so
  a person's own hook — even a guarded `carto` one — stays.

## Done — step 1b, the narrow-lookup fix

A measured Copilot session found `carto large-functions` costing ~6x
`find | xargs wc -l | sort` and returning no functions: 15 File, 4 Class,
1 Test, topped by a generated 14,726-line `.d.ts`. Design and every number:
**`docs/design/compact-output.md`**.

- `7382381` — compact rows by default for list commands (`compact.py`, the
  one place a row is shortened); `--detail full` for the whole row. The
  repo-relative conformance check now anchors each field of a row: under the
  old check, rows written before relativising still passed.
- `6f251b4` — `large-functions` defaults to `Function` (methods included; the
  graph has no Method kind), `--kind` repeats to widen, generated and `.d.ts`
  files excluded and counted, `--include-generated` to rank them.
- `1cdd2ca` — one-line summaries for `impact` and `review-context`.
- `27081cc` — skills say when a shell command is cheaper than carto.

Headline, claude-mem (991 files), default invocation: `large-functions
--min-lines 80 --limit 20` 10,897 → 2,577 chars (2,725 → 645 tokens), now
functions; `flows --limit 20` 19,465 → 2,223; `communities` 13,202 → 1,157;
`review-context` 45,476 → 19,048. `impact` was left open here (500 results
plus every edge, 506 KB); step 1c below closed it.

## Done — step 1c, `impact` short without hiding scope

The user's constraint: a short list must never let an agent believe it has
seen everything. Design, ranking rule, the direct-dependents decision, and
every number: **`docs/design/compact-output.md`, "`impact`"**.

- `6544aaa` — default `--limit 20` (was `--max-results 500`); items ranked
  direct first, then score, then name, on both engines; every affected file
  listed with counts whatever the limit; exact `totals` (items, direct,
  files, edges by kind); `truncated`/`page.total_estimated` exact;
  `data.see_all` names the command for everything; the summary announces
  direct dependents beyond the limit. `engine/tests/test_impact_scope.py`
  (14 tests, each seen failing with its guarantee removed) and a conformance
  op. The old default's file list was built from the kept nodes, so it
  understated scope (79 of 99 files for SessionStore.ts).
- `644ccf0` — skills: impact is ranked; read `truncated`; the file list is
  complete; `see_all` or `--limit <totals.direct>` for the rest.

claude-mem, default invocation: SessionStore.ts 506,392 → 10,967 chars
(126,598 → 2,742 tokens); TelegramWrapupNotifier.ts 78,826 → 7,038;
CorpusBuilder.ts 8,619 → 3,052. Hub files pay for the file list: logger.ts
(463 files) is 34,386.

## Done — step 2, Cartograph's files excluded locally (decision 3)

- `7447759` — `carto install` writes a `# cartograph (managed)` …
  `# end cartograph` block into the file `git rev-parse --git-path
  info/exclude` names (so a linked worktree writes the shared one). It lists
  `/.cartograph/` and exactly what that run wrote: each
  `/.github/skills/<skill>/`, `/.github/hooks/cartograph.json`, and the
  instruction file when written. The block is replaced whole, so a re-run
  changes nothing. Paths git already tracks are named in the output and left
  out. Not a git work tree: one line, nothing written. `.gitignore` is no
  longer edited — `ensure_repo_gitignore_excludes_crg` is gone — and a block an
  earlier release put there stays. `carto uninstall` removes the block only.
- Tests: `engine/tests/test_git_exclude.py` (13, real `git init` repos, one
  end to end through `python -m cartograph install` checking `git status
  --porcelain` is empty); each seen failing with its behaviour removed.
- The extension needed no change: it runs `carto install`.
- This repository tracks `.github/skills/` but not `.github/hooks/`; install
  here reports the skills as tracked and excludes the hook file.
- Not changed: `carto uninstall` still deletes the skill files even where git
  tracks them, and still removes the `# Added by cartograph` block from
  `.gitignore` that earlier releases wrote.

## Done — step 3, VS Code's language models as the summary fallback (decision 1)

Order: `copilot -p --model auto`, then the extension through `vscode.lm`,
then structural.

- **Engine.** `mem sync --summarise --hand-off {no-cli,always}` lists the
  sessions awaiting a summary (`awaiting_summary`, same selection and cap of 3
  as `--pending`) and writes nothing, when no CLI host is on PATH (`no-cli`) or
  always; `--no-host-agent` and the `SUMMARISE_MARKER` depth cap still win.
  `mem summarise --session X --brief-only` returns the brief `brief()` builds,
  replies included, and writes nothing. `--answer-file PATH --summarised-by
  vscode-lm:<model-id>` stores the answer through the host-answer path:
  `split_title`, `clip_body`, one summary per session, `host-agent`, the label
  as `platform_source`, `cost:host_calls` +1 (an empty answer is structural,
  and still counted). The label must match `vscode-lm:…`. `mem status` gains
  `latest_summary_by`. Both commands take `--vscode-user-dir` /
  `--vscode-workspace-dir`, so a brief in a remote window keeps its replies.
- **The Chat catch-up hook hands off.** `SessionStart` from Chat now runs
  `mem summarise --pending --hand-off no-cli`; without it, a VS Code-only
  machine with hooks on would write structural summaries before the extension
  saw the sessions. The CLI's hook is unchanged — `copilot` is there.
- **Extension.** `extension/src/summaries.ts` holds every `vscode.lm` call.
  Settings `cartograph.summaryHost` (`auto` / `vscode` / `cli` /
  `structural`) and `cartograph.summaryModel` (default `auto`: Copilot Chat
  registers its Auto model with `vscode.lm` as vendor `copilot`, id `auto` —
  `microsoft/vscode` `extensions/copilot/src/extension/conversation/vscode-node/languageModelAccess.ts`
  and `platform/endpoint/node/autoChatEndpoint.ts`, commit `3ba86b4`). Background runs never raise
  VS Code's consent dialog: when consent was never asked, a notification asks
  once per window; **Sync Memory** (a user action) may raise it directly.
  NoPermissions, Blocked or NotFound → structural
  for that session and the rest, VS Code's models ruled out for the window,
  one notification. (The named model missing did the same until 2026-09-30;
  it now falls back to another model — see below.) A timeout (90 s) or other error → structural for that
  session only. No Copilot models at all (not signed in, not activated yet) →
  nothing written; the sessions wait. The tooltip names the latest summary's
  writer. `engines.vscode` raised `^1.85.0` → `^1.90.0` (the `vscode.lm`
  API's first stable release).
- Tests: `engine/tests/test_mem_summary_handoff.py` (27), each seen failing
  with its behaviour removed (18 mutations). No TS test runner: the extension
  side is compiled, not run.

**Needs a live window test** (none of this has run in VS Code):
1. A VS Code-only machine (no `copilot` on PATH), Chat hooks on: two Chat
   sessions of 2+ prompts, start a third. Expect the Allow notification,
   then VS Code's consent dialog naming the justification, then `carto mem
   show` of the summary row: `platform_source: vscode-lm:auto`,
   `host-agent`, and the tooltip "summaries via VS Code (model auto)".
2. Same, refusing in VS Code's consent dialog: expect one warning,
   a structural row, and no further prompts until reload.
3. Hooks off (logs path), `cartograph.summaryHost: vscode` with `copilot`
   installed: expect the sessions summarised through VS Code, not the CLI.
4. A Remote SSH window: the same as 1, with the engine on the remote.
5. Whether `selectChatModels({vendor: "copilot"})` really lists `id: "auto"`
   on the user's Copilot plan and VS Code version — now read from the
   **Cartograph** output channel's `models offered:` line.

## Done — step 4, the bundled embedding model (decision 2)

`mem search` is hybrid (FTS5 + vectors, merged by rank) wherever the payload's
model is found, and `keyword` with `data.semantic_unavailable` where it is not.
Design, choices and the measured table: **`docs/memory-design.md`,
"Meaning-based search"**.

- **Model**: all-MiniLM-L6-v2, upstream's int8 ONNX export, Apache-2.0,
  revision `1110a243`, sha256-checked; fetched by `build-payload.py` (`MODEL`)
  into `payload/model/`; `--model-only` fetches it into `build/model` for the
  tests. Recorded in NOTICE and PROVENANCE.
- **Runtime**: onnxruntime 1.30.0 + tokenizers 0.23.2 (no hub client) + numpy
  2.5.3 + sqlite-vec 0.1.9, frozen with the engine; ORT's unused C library pruned.
  `OnnxEmbeddingProvider` in `embeddings.py`; `get_provider()` picks it first,
  so the graph's `carto embed`/`search` use it too.
- **Found by** `CARTO_EMBEDDING_MODEL_DIR`, set by both launchers and the
  extension like the grammar variable.
- **Embedded** by `mem sync` (all), `mem add`/summaries and `mem search` (≤256
  missing rows each) through `MemoryStore.embed_missing`; never by the capture
  hook. Old stores backfill by use; a foreign-model index is rebuilt.
- **The build interpreter** must load SQLite extensions; python.org's macOS
  build cannot, so `build-payload.py` falls back to uv's CPython 3.12 and CI's
  macOS job installs uv.
- **Measured on this Mac, frozen**: `.vsix` 32.8 → 62.1 MB; `mem search` keyword
  0.19 s / 67 MB, hybrid 0.27 s / 163 MB (median of 5, warm file cache);
  6.4 ms per embedded row.
- **Offline proof**: `scripts/ci_smoke.py` now runs the frozen binary under an
  OS network block (sandbox-exec / `unshare --net` / a Windows firewall rule),
  asserts hybrid finds a reworded memory keyword misses, and prints the cost
  table; every CI platform job runs it. `docker/assert.sh` asserts the same
  with `--network none`. Tests: `engine/tests/test_mem_embeddings.py` (12).

**Unproven until CI runs:** everything on linux-x64 and win32-x64 — that
PyInstaller collects ORT there, that pruning `libonnxruntime.so`/`onnxruntime.dll`
is safe, that `unshare --net` works in the container with the added
capabilities, that the firewall rule applies, the Windows need for
`MSVCP140.dll` (ORT imports it; the runner has it, a bare machine may not), and
all their timings.

## Done — 2026-09-30, the Linux floor, and two honest status lines

0.8.0's linux-x64 embedding stack did not load on the user's Ubuntu 22.04
(glibc 2.35): the payload bundled Debian 12's `libstdc++.so.6` (`GLIBC_2.36`).

- **Build** (`build(linux)` commit): CI and `docker/Dockerfile` build on
  `quay.io/pypa/manylinux_2_28` with uv's CPython; tree-sitter-language-pack is
  compiled from source on Linux (its only wheel is `manylinux_2_34`);
  `scripts/check-glibc.py` fails above 2.31; CI job `linux-targets` runs the
  shipped `.vsix` offline on `ubuntu:22.04`, `ubuntu:20.04`, `debian:11`.
  Floor, reasons and evidence: `docs/packaging.md`, "Supported Linux".
- **One-line failure reason** (`fix(mem)`): `embedding_failure_reason` in
  `embeddings.py`; the store records it in `mem_meta` so `mem status` names it;
  the traceback only under `CARTO_DEBUG=1`.
- **Status line** (`fix(extension)`): `describe()` reads `mem status`'s
  persisted `capture_*` lines and counts, not the latest sync.

## Done — 2026-09-30, a missing model no longer means structural, and a log

On the Remote SSH VM (VS Code 1.138, Ubuntu 22.04, Chat hooks off by policy,
`summaryHost: vscode` likely in Windows User settings) **Sync Memory** gave
session f2f66c3b a structural summary with no consent dialog, no Allow
notification and no recorded reason. Likeliest cause, from the code: Copilot
did not offer a model with id/family `auto`, and a missing model ruled VS
Code's models out for the window. **Unproven.**

- **Engine** (`feat(mem)`): the reason for each structural summary is kept in
  `mem_meta` (`summary:fallback_reason:<session>`) — the engine's own, or a
  caller's via `--fallback-reason` (needs `--no-host-agent`; on `mem summarise`
  and `mem sync --summarise`). `mem status` → `latest_summary_by: structural
  (<reason>)`. Tests: 9 more in `test_mem_summary_handoff.py`, seen failing
  under three mutations.
- **Extension** (`fix(extension)`): `modelChoice.ts` — pinned setting, else
  Auto, else a light model (whole-word mini/nano/luna/flash/haiku/lite/small in
  family → id → name, `maxInputTokens` ≥ 12,000), else the first offered;
  checked by `node extension/test/model-choice.js` after compiling (13 cases).
  A pinned model not offered is reported once and replaced, never structural.
  **Cartograph** output channel + **Cartograph: Show Log**. Every structural
  summary the extension writes carries `--fallback-reason`; the ruled-out
  warning and the Sync Memory result offer "Show Log".
- Re-summarising a structural summary later: proposed, not built —
  `docs/memory-design.md`, "Open questions".

**Needs the live retest** on the VM, before anything else about summaries:
1. **Cartograph: Show Log** after **Sync Memory** with a new finished session:
   read the `models offered:` line. It settles whether `auto` is offered on
   this VS Code/plan, and which model was chosen instead.
2. Expect VS Code's consent dialog (Sync Memory is user-initiated), then a
   `host-agent` row labelled `vscode-lm:<chosen id>` and the tooltip naming it.
3. If it is still structural: the log line for the session names the reason
   and any `LanguageModelError` code, and `carto mem status` shows
   `structural (<that reason>)`.
4. Session f2f66c3b keeps its structural summary (one per session); it has no
   stored reason, since it predates this.

## Done — 2026-09-30, a session settles by its last message

On the VM a finished session was never handed over: VS Code rewrites
`chatSessions/<id>.jsonl` for UI state on reload, the companion re-copies it,
and sync judged "still active" by file mtime. Now the last message's own
timestamp decides, across all of a session's logs, with mtime only as a
stated fallback; `mem sync --summarise` returns `waiting`
(`{session, last_message_at, settles_at}`), the log line prints it and Sync
Memory says so. Fields and limits: `docs/memory-design.md`, "When a session
has finished". Tests: `engine/tests/test_mem_settle.py` (16, each seen failing
under 11 mutations) and `node extension/test/waiting.js` after compiling.
Not yet seen on the VM.

## Done — 2026-10-01, four findings from a user-run A/B (Copilot CLI, gpt-5.4-mini)

- **Version** (`fix(version)`): `carto --version` prints `cartograph 0.8.4
  (engine fork of code-review-graph 2.3.8)`. The release comes from
  `extension/package.json` (`engine/cartograph/release.py`); `build-payload.py`
  stamps it into the bundle (`cartograph/RELEASE`) and `PAYLOAD.json`,
  `build-vsix.sh` refuses a payload stamped otherwise, `ci_smoke.py` asserts
  it, the skew check compares the release word exactly
  (`node extension/test/version.js`). Proven on a frozen darwin-arm64 build.
- **Stale skills** (`fix(install)`): install and uninstall remove the copies
  pre-0.6.0 releases wrote to `.claude/skills`/`.agents/skills` only when
  provably Cartograph's (pack name, SKILL.md only, text one of the shipped
  versions in `engine/cartograph/skills_shipped.json`, which
  `install-skills.py` builds from git history); anything else is kept and
  named. `engine/tests/test_legacy_skills.py`, 6 mutations each caught.
- **Routing** (`fix(skills)`): "who calls X / what would break" now routes to
  refactor-safely (callers_of, references_to, tests_for, impact). Descriptions
  1,849 → 1,332 chars in total; tests pin routing and a 1,400 ceiling.
- **Overview cost** (`perf(architecture)`): compact `architecture` rows with
  each community's directories and coupled pairs; the skill answers an
  overview with that one call. claude-mem: the skill's overview path 16,591 →
  1,671 chars (4,149 → 418 tokens). `docs/design/compact-output.md`.
- **Not re-measured:** the A/B itself. T1 recall (+20%) was not addressed.

## Done — 2026-10-01, the second A/B: an accurate overview, and T3's route back

The user's next A/B (Copilot CLI, gpt-5.4-mini, 5 alternating rounds, isolated
COPILOT_HOME) on a repository that is mostly docs and templates found two
regressions from v0.8.6:

- **T2 overview wrong 5/5 with carto** (4/4 right without): the skill said the
  overview was one `architecture` call, which describes only parsed code — a
  few Python scripts here. `architecture` now leads with `layout`: every
  tracked file by top-level directory and kind (code/docs/config/other), the
  graph's share, the root docs to read first, and a note when under half is
  code (`engine/cartograph/layout.py`, rows in `compact.py`, file list shared
  with the build via `incremental.repository_files`). The skill says the graph
  covers parsed code only and, when the note is present, to read the docs it
  names. claude-mem: 1,671 → 2,819 chars, no note (65% code).
- **T3 "largest functions" never used carto** (0/5; 2/3 before v0.8.6): the
  trim had dropped it from every description. explore-codebase's description
  names it again, and its body says largest *functions* are not a shell
  question. Descriptions 1,332 → 1,348 chars.
- Routing tests now hold each A/B phrasing to its skill
  (`TestSkillRouting::test_ab_tasks_*` in `engine/tests/test_skills.py`).
- **Not re-measured:** the A/B itself. T1 and T5 are pinned by the routing
  tests, not re-run.

## Done — 2026-10-01, the third A/B: top 10 means ten, and the working tree

The user's A/B on T3 ("the largest functions … top 10 with their file and
line count", Copilot CLI, gpt-5.4-mini, 5 rounds): with carto 0/5 fully right
(four 7/10, one listed only 6), without 2/5, though carto cut cost 83%. A
hidden `--min-lines 50` turned `--limit 10` into 6 rows, and the graph held
tracked files only, so an untracked skill directory's scripts were invisible.

- **Working tree.** Build, layout and coverage list tracked files plus
  untracked ones git does not ignore (`get_untracked_files`, `git ls-files
  --others --exclude-standard`). `carto update` and the Stop-hook refresh add
  untracked files that are new or edited (hash-checked), drop deleted ones
  through the existing reconciliation, and drop ones since git-ignored (one
  `git check-ignore`); `get_changed_files` lists untracked files, so
  `detect-changes` sees them. Existing graphs gain them on the next update.
- **Top N.** `large-functions` applies no threshold unless `--min-lines` is
  given; `--limit` defaults to 20; the summary names what was applied and
  `page.total_estimated` is the exact count.
- **Coverage.** `large-functions`, `search`, `query`, `refactor`,
  `dead-code` carry `data.coverage` (`layout.coverage`), e.g. `searched 991
  of 996 code files; not covered: 5 no parser (.html)`, and the summary says
  when it is partial. Skills tell the agent to say so in the answer.
- Design, measurements and the mutation table:
  `docs/design/compact-output.md`, last section. Tests:
  `engine/tests/test_working_tree_coverage.py` (20, 18 mutations each caught).
- **Not re-measured:** the A/B itself; a repository with a large untracked,
  not-ignored tree.

## Done — 2026-10-02, the fourth A/B: test methods, a moved checkout, `--format` on writes

The user's T3 re-check on 0.8.8 (Copilot CLI, gpt-5.4-mini, 5 alternating
rounds, the repo copied to `/tmp/carto-ab4/…`): 4/5 carto runs missed the same
two reference entries, both test methods; every answer warned, wrongly, that
the ranking was partial (`searched 0 of 71 code files`); `carto update
--format json` was a usage error.

- **Test functions rank.** `large-functions` defaults to `Function` and
  `Test`, minus JS/TS `describe`/`suite` blocks (containers, like classes).
  Rows say `Test`; the summary says `(tests included)`. Other Function-only
  filters (dead-code, refactor suggest, review's test gaps) are deliberate.
- **Moved or remounted checkouts.** Reads compare repo-relative paths against
  the root the graph was built at (`repo_paths.register_anchor`, inferred,
  half the sampled paths must agree); `update`/`build` rebase stored paths in
  one transaction first. Fixed beyond coverage: absolute old paths in rows,
  `importers_of`, `architecture`'s graph share, `detect-changes`, flow source,
  `update` refusing. Schema unchanged. Rebase costs ~10 s on claude-mem, paid
  at each write after a mount switch.
- **`--format json|text`** on build, update, postprocess, embed, with
  precondition exits; visualize/wiki/forget/repos still text-only, and the
  README and capabilities convention say so.
- Before/after table, measurements and the mutation table:
  `docs/design/compact-output.md`, last section.
- **Not re-measured:** the A/B itself; no real Dev Container run.

## Done — 2026-10-05, the fifth A/B: nested components, and what an impact row depends on

The user's A/B on 0.8.9 (vault `20 Projects/FRQ/report6.md`): T2 overview
with carto 2/5 correct vs 4/5 without — answers missed the skills and MCP
server nested in `template/`; T5 4/5 vs 5/5 — one answer read impact's
importers as call-site breakage, and one run tried `carto query impact`.

- **Layout:** a top-level directory with ≥25% of files or ≥50% of code files
  (not a test tree) names its sub-directories and components inside it
  (`skills:`, `server:`, `app:`, `tests:`, `hooks:`, `ci:`).
  explore-codebase: name them, and take connections from the README/docs,
  not community coupling.
- **Impact rows** say the relation (`direct | calls add_node | …`, `direct |
  imports graph_store.py | …`, `transitive | calls ingest | …`); the summary
  splits direct by relation (`9 direct: 4 call, 5 import only`), says
  import-only dependents are not broken by a signature change, and points to
  `query callers_of <file>::<name>`. 0.6.1's guarantees unchanged.
  refactor-safely: callers_of + tests_for first, impact only for wider reach.
- **Wrong spellings** (`query impact`, `query large-functions`, `query
  callers`, bare `callers_of`) → usage error naming the right command, as
  `error.remediation`.
- Sizes and the mutation table: `docs/design/compact-output.md`, last section.
  impact grows 11–33%; architecture +469 chars on the A/B-shaped fixture,
  unchanged on claude-mem.
- **Not re-measured:** the A/B itself. No version bump.

## The user's A/B measurements (Copilot CLI, gpt-5.4-mini) — summary

Reports are in the vault: `20 Projects/FRQ/MCPs/report-carto-1oct2026.md`,
`report2.md`, and `20 Projects/FRQ/report3.md` … `report6.md`. Read billing
(`totalNanoAiu`) and input+cached+output, never input alone; with five runs per
side, cost noise is large, so judge cost by non-overlapping ranges and weigh
correctness first.

| Task | Result on the latest version measured |
|---|---|
| T1 recall ("have we already decided…") | 0.8.6: with 5/5 correct vs 0/5 without (isolated Copilot homes) |
| T2 repository overview | 0.9.1: with 5/5 correct vs 3/5; −27% billing, 4 vs 9 tool calls (nested components named in the layout row) |
| T3 largest functions | **0.8.9: with 5/5 fully correct vs 1/5; −86% billing, 2 vs 17 tool calls; every "with" run cheaper than every "without" run; coverage "searched all 71 code files" in the repo and in a moved copy** |
| T4 largest files | carto correctly not used; tie |
| T5 callers / blast radius | **0.9.2: with 5/5 correct vs 2/5; 5/5 answers state what breaks per kind of change (rename / order / optional) vs 0/5 explicit without; cost ≈ (+5%, overlapping)**. Gap seen: `query tests_for add_node` returned nothing although two test files call it (callers_of listed them) — open |
| T6 free choice | tie |

Defects the measurements found, all fixed: code-only overview (0.8.7), lost
large-functions route (0.8.7), hidden `--min-lines 50` and untracked files
missing from the graph (0.8.8), test methods excluded, absolute paths breaking
a moved checkout, no `--format` on build/update (0.8.9). Open, cosmetic:
`update` reports `total_nodes: 0` when nothing changed.

## NEXT TASK

1. **Remote SSH acceptance of 0.4.1** on the user's VM. 0.4.0 proved the
   companion carries Chat history across; 0.4.1 fixes what that run found.
   The in-notice install of the companion has since worked on the VM (0.4.1).
2. **Transcript-aware summaries.** Both hosts send `transcript_path`, which
   holds the agent's replies. Summaries built only from prompts can only say
   what a person typed; the replies say what was concluded.

## Then, in rough order

1. **Read the CI numbers** for linux-x64 and win32-x64 from the job summaries
   into `docs/memory-design.md`, and act on any failure of the offline step.
2. **`carto mem timeline` and `mem show`** — specified in T10, cheap, and
   `timeline` answers "what happened in this session", which search cannot.
3. `detect-changes` and `dead-code` exit via `SystemExit(0)` rather than
   returning. Harmless in a shell, awkward for in-process callers.

## Not code, and blocking the acceptance test

A Windows machine has the repo but the test never ran. `install.ps1` has never
executed anywhere, the extension has never activated in a real VS Code window,
Copilot Chat's own discovery of the placed skills is unconfirmed, and the binary
is unsigned. See `docs/packaging.md` and `docs/acceptance.md`, which carry
explicit unverified lists rather than implying coverage.

## Test suite: 1 known failure, not 56

`test_custom_languages.py::TestParserIntegration::test_e2e_nodes_and_edges`.
The erlang grammar is fetched at runtime and its current version nests a remote
call differently, so the assertion encodes an older grammar shape. Nothing in
this fork touches that path. Deliberately NOT skipped — the manifest is
reachable, so a network guard would mislabel a real signal.

```bash
cd engine && .venv/bin/python -m pytest tests/ -q --timeout=300
```

Nothing is ignored any more: the MCP server and its tests are gone (4b).

## Hooks

`carto hook <event>` (`engine/cartograph/hook.py`) is what hosts call; agents
never do (it is in `_NOT_AGENT_FACING`). Events are named for the job —
`session-status`, `file-update`, `prompt-capture` — with Copilot's host
spellings aliased onto them.

`prompt-capture` is the only event that reads the host's stdin payload, and the
only one that writes: it records the submitted prompt into the `mem` store in
process (`engine/cartograph/mem/ingest.py`), verbatim, ~1 ms of work inside a
~160 ms interpreter start. What it refuses is the design — acknowledgements,
payloads with no prompt in them, and anything past a per-session cap. Wired for
Copilot CLI and Copilot Chat through `UserPromptSubmit` in
`.github/hooks/cartograph.json`; both payloads are recorded in
`docs/copilot-hooks.md`.

**The hook protocol is not the query protocol.** In a hook, exit `2` means
*blocking feedback to the model*, not "precondition failed". `hook.py` never
touches the envelope helpers and always exits 0; a test walks its AST to keep
it that way. `spawn_detached` is the only detach path — `&` does not background
on Windows, and Copilot has no async hook type (30s timeout) against a build
that takes minutes. The Windows branch is asserted at the flag level only;
nobody has run it on Windows.

## The skills pack, and how it stays true

Six skills in `skills/<name>/SKILL.md`, copied into `.github/skills/` by
`scripts/install-skills.py`.
Copies, not symlinks: git on Windows checks a symlink out as a text file
containing its target path, which a host reads as a skill body and ignores.

It is also synced into `engine/cartograph/skills_data/`, the package data the
engine ships so `carto install` can write the pack on a machine that never
cloned this repo.

**Never hand-edit a copy** — edit `skills/`, then re-run the installer.
`--check` catches stale, missing and orphaned copies and runs in `verify.sh`.

`carto install` writes the pack to `.github/skills/`, byte-identical to
canonical, and **registers no MCP server** — there is none.

`contracts/capability-v1/check_skills.py` extracts every `carto` line from
every skill body and validates it against `carto capabilities`. This is not
decoration: the designed skill drafts named five commands and flags that do
not exist, and every one was caught this way rather than by review. If you add
a skill, the checker holds it to the same standard automatically.

## Gotchas that cost time already

- **Run from the repo root**, not `engine/`. The conformance manifest uses
  `cwd: engine`; running from inside `engine/` makes it look for `engine/engine`.
- **`PYTHONPATH=engine`** is needed when invoking `python -m cartograph`
  from the repo root (the package is not pip-installed). The venv is at
  `engine/.venv`.
- **Repo-root divergence is real.** `engine/` has no `.git`, so `find_project_root`
  walks up to `cartograph/`. Build and query with the *same* `--repo` or you will
  read an empty graph and conclude the code is broken. This cost two
  misdiagnoses.
- **Never rename the `.code-review-graph` literals.** They name state written
  *before* the rename, so recognising the old spelling is their entire purpose.
  They are `LEGACY_DATA_DIR` / `LEGACY_DB_FILE` in `incremental.py`, with a
  comment saying so. A blanket sed renamed them once and silently disabled the
  legacy migration and half of `uninstall`.
- **Use an ABSOLUTE `PYTHONPATH` when testing hooks.** `spawn_detached` runs
  the child with `cwd` set to the target repo, so a relative `PYTHONPATH=engine`
  resolves against that repo, the import fails, and the failure is invisible
  behind `DEVNULL`. This looks exactly like "detaching does not work".
- **Token budget semantics** are in `docs/design/token-budget.md`. Two traps
  it records: the size block is part of what it measures (so `_with_size`
  iterates to a fixed point), and truncation flags cost characters too (so they
  are installed *before* fitting, not after).
- The engine graph for this repo is built and current (~270 files).
  Rebuild: `PYTHONPATH=engine engine/.venv/bin/python -m cartograph build --repo .`

## After that, in rough priority

1. **The memory side** (`memory/`) has not been started. Engine-first was the
   decided build order.
2. **The `.vsix`** — the primary delivery vehicle, and the acceptance test:
   a fresh machine with only VS Code.

## Decided, do not relitigate

- Storage is SQLite + `sqlite-vec`. FalkorDB rejected (SSPLv1, needs a daemon);
  Chroma rejected (`chromadb` Node client has no embedded mode).
- Semantic search ships in the **base** install via ONNX (~150–300MB/platform
  after pruning; `onnxruntime-node` is 287MB unpruned).
- Summarisation shells out to the host agent (`copilot -p`), **budgeted and
  visible**: recursion guard, per-session cap, `summary_source` on every memory.
- Build order is engine-first. claude-mem compiled tree-sitter grammars from C
  at runtime, needing a toolchain no locked-down machine has — so it will call
  the engine's precompiled parser instead.
- The `.vsix` is the primary delivery vehicle; installing it also serves
  Copilot CLI.
