---
tags: [hooks, copilot, cartograph]
updated: 2026-09-29
---

# Copilot hooks

How memory capture and session summaries reach **Copilot CLI** and **Copilot
Chat** — the only AI hosts the target environment allows. Everything below was
read off real hook payloads on 2026-09-29, from Copilot CLI 1.0.82 and VS Code
1.139.1 on macOS, by pointing probe hooks at a scratch repository. Re-probe
rather than trust this when either host changes.

## What the hosts do

| | Copilot CLI | Copilot Chat (VS Code) |
|---|---|---|
| Reads `.github/hooks/*.json` | yes — **only in a trusted folder** | yes |
| Schema accepted | its own (`version`, camelCase, `bash`) **and** VS Code's | VS Code's (PascalCase, `command`) |
| `SessionStart` / `UserPromptSubmit` / `Stop` | yes | yes |
| `SessionEnd` | yes, once per session | **no such event** |
| Payload keys | `session_id`, `prompt`, `cwd`, `hook_event_name`, `transcript_path` | same |
| Environment marker | `COPILOT_CLI=1`, `COPILOT_PROJECT_DIR` | none |
| `PATH` the hook sees | the shell that started the CLI | the **extension host's** — not a terminal's |

Consequences, each of which is now code:

1. **One file, VS Code schema.** The CLI runs files in *both* schemas, so a
   CLI-schema file beside a VS Code-schema file fires every CLI event twice.
   `carto install --platform copilot` writes one file,
   `.github/hooks/cartograph.json`. Capture also refuses an exact duplicate
   prompt within a session, for anything that still fires twice.
2. **The host is named by the environment.** The payloads are identical, so
   `--host copilot` is narrowed to `copilot-cli` when `COPILOT_CLI` is set and
   `copilot-chat` otherwise (`hook.resolve_host`).
3. **Summaries also happen at session start.** Chat never says a session
   ended, so `SessionStart` runs `carto mem summarise --pending`, which
   summarises up to three earlier sessions that have none — excluding the one
   starting. This also catches a CLI session that crashed before `SessionEnd`.
4. **The graph refreshes on `Stop`, not `PostToolUse`.** `Stop` is once per
   agent turn. `PostToolUse` fires for reads too, VS Code ignores matchers,
   and the CLI's agent writes files through `Bash` as often as through an edit
   tool — so filtering by tool name misses edits and not filtering starts a
   dozen concurrent updates per turn.
5. **The hook finds `carto` without PATH.** The extension puts
   `~/.cartograph/bin` on the PATH of VS Code *terminals*; Chat hooks do not
   run in one. Every hook line appends `${CARTO_HOME:-$HOME/.cartograph}/bin`
   to PATH before its guard. Before this, every Chat hook exited silently.
6. **No `session-status` line for Copilot.** VS Code parses a hook's stdout as
   JSON; the orienting line Claude Code receives would be a parse error there.

## Organisation policy can switch Chat hooks off

`chat.useHooks` can be set by organisation policy. On the first Linux test
machine it was **off and "Managed by organization"**: VS Code never ran the
hook, the hook worked when run by hand, and nothing said why. Where that
policy applies, hooks cannot deliver Chat memory at all. Copilot Chat still
writes `workspaceStorage/<id>/GitHub.copilot-chat/transcripts/<session>.jsonl`
with hooks off. See `docs/verify-memory.md`, Findings.

**Copilot's transcript is not enough with hooks off.** Measured on macOS with
`chat.useHooks` off (VS Code 1.139.1): `transcripts/<session>.jsonl` held
`session.start` and nothing else. VS Code's own chat store,
`workspaceStorage/<id>/chatSessions/<session>.jsonl`, held every prompt and
reply under the same session id. It is a snapshot line (`kind` 0) followed by
patches (`kind` 1 sets the value at path `k`, `kind` 2 appends to the list at
`k`), and sync replays it.

**The fallback: `carto mem sync`** (`engine/cartograph/mem/sync.py`). It reads
both Chat logs for workspaces opened on the repository and CLI
`session-state/*/events.jsonl` whose `session.start` names the repository as
its git root, and imports prompts not already recorded. Session ids in the
logs equal the hooks' ids, so it never double-records. Imported-versus-already-
recorded is the empirical test of whether hooks fire, stored and shown by
`mem status` as `capture_copilot_chat` / `capture_copilot_cli`. The extension
runs it on activation, 30 s after a transcript changes, and every 10 minutes,
with `--summarise` for sessions whose log has been quiet for 30 minutes.
Proven on this Mac against real logs: with the hooks' store, 7 of 7 logged
prompts already recorded (`hooks`); with the store removed, the same 7
recovered with the same session ids (`logs`). And through the extension
itself, in a VS Code profile with `chat.useHooks` off: a Chat prompt reached
memory 75 seconds after it was sent, `capture_copilot_chat: logs`, with no
hook involved.

**Open: Remote SSH.** `chatSessions/` is VS Code core's store, and under Remote
SSH it may live on the client machine rather than the remote where the
extension and engine run. Unverified; see `docs/verify-memory.md`.

## The CLI's folder trust

The CLI loads repository hooks only in a folder the person has trusted (its
log says `Loading repo hooks in prompt mode (folder is trusted or opt-in set)`).
Interactive `copilot` asks once per folder. **`copilot -p` in an untrusted
folder runs no repository hooks at all, silently.** That is the CLI's security
model and Cartograph does not work around it.

## Proven end to end, live

In a scratch repository with only `.github/hooks/cartograph.json` installed and
`carto` reachable only through `~/.cartograph/bin` (not on PATH):

- Copilot CLI, two prompts in one session (`-p`, then `--continue -p`): both
  captured as `copilot-cli`; `SessionEnd` spawned a summary written by a real
  `copilot -p` call, `summary_source: host-agent`, with a correct `DEAD ENDS`
  line.
- Copilot Chat, via `code chat`: prompts captured as `copilot-chat`.
- A CLI session killed twice mid-answer (no `SessionEnd`): the next Chat
  `SessionStart` summarised it, `host-agent`.
- The summariser's own Copilot session captured nothing: zero rows containing
  the summariser's brief.
- `Stop` built the graph with nobody running `carto build`.

## Not yet verified

- **Windows.** The `windows` (VS Code) and `powershell` (CLI) forms are
  generated and never run.
- **Linux.** The same POSIX line as macOS; the frozen binary's re-entry was
  fixed in the same change and has not been run inside a `.vsix` there.
- **Chat sessions of more than one prompt.** `code chat` opens a new session
  per call, so the live Chat run proved capture and catch-up but not a Chat
  session reaching `MIN_PROMPTS` on its own. The logic is the CLI's, tested.
- **`chat.useClaudeHooks`.** If someone enables it, VS Code also reads
  `.claude/settings.json`; the duplicate check covers capture, but the Claude
  file's `session-status` line would reach Chat's JSON parser.

## Follow-up worth doing

Both hosts send `transcript_path` — the conversation *including the replies*.
Summaries today see only the prompts, so `DECIDED` is only as good as what a
person typed. Reading the transcript would let a summary say what the agent
actually concluded.
