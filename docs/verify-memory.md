---
tags: [runbook, memory, cartograph]
updated: 2026-09-29
---

# Verify memory works

A step-by-step check that Cartograph is recording Copilot sessions on a
machine, run inside VS Code. Do the steps in order; each one says what to
expect and where to go when it does not match.

First run: Linux x64 VM over VS Code Remote SSH, VS Code 1.138.0, as root,
2026-09-29. What it found is under [Findings](#findings).

> Run every command in a **VS Code terminal opened after the extension
> installed** — that is the terminal whose PATH has `carto` on it.

## Step 1 — Install the `.vsix`

```
gh release download v0.2.0 -R danemil/cartograph -p '*linux*'
code --install-extension carto-linux-x64-0.2.0.vsix
code --list-extensions --show-versions | grep -i carto
```

**Expect** `cartograph.cartograph@0.2.0`. Use the `.vsix` for the machine's
platform (`linux-x64`, `win32-x64`, `darwin-arm64`); the release page has all
three. Over Remote SSH, install it into the remote — the extension, the engine
and the memory store all live there.

## Step 2 — Open the repository and check what was installed

Open the repository folder, run **Developer: Reload Window**, open a **new**
terminal, then:

```
code --list-extensions --show-versions | grep -i carto
ls .github/hooks/
carto mem --help
carto mem status --format text
```

**Expect** `@0.2.0`, `cartograph.json`, a subcommand list including
`summarise`, and *"No memory store"* — the store is created by the first
captured prompt, so its absence here is correct.

- No `cartograph.json` → run **Cartograph: Install Skills and Hooks into
  Workspace** from the Command Palette.
- `carto: command not found` → the terminal predates the install; open a new one.

## Step 3 — Send two prompts in Copilot Chat

Open Copilot Chat, choose **Agent** mode, and in **one** chat send:

> We need to decide where to keep build caches for this repo. What are the
> options? Answer briefly.

> We tried storing the caches in /tmp but they were wiped on every reboot, so
> that's a dead end. We're going with a .cache folder in the repo instead.
> Acknowledge briefly.

Then:

```
carto mem status --format text
carto mem search --query "caches" --doc-type prompts --format text
```

**Expect** 2 observations, both `copilot-chat`. With hooks working they
appear at once; from 0.3.0, when hooks are blocked, the extension imports them
from Copilot's own log within about a minute — run **Cartograph: Sync Memory
from Copilot Logs** to do it immediately. Still *"No memory store"* → Step 4.

## Step 4 — Run the hook by hand

Runs the exact command VS Code should have run, with a fake payload. It
separates "the hook is broken here" from "the host never ran it".

```
git rev-parse --show-toplevel
which carto
code --version | head -1
echo '{"session_id":"manual-test","prompt":"Manual hook test from the terminal"}' | sh -c "$(python3 -c 'import json;print(json.load(open(".github/hooks/cartograph.json"))["hooks"]["UserPromptSubmit"][0]["command"])')"
carto mem status --format text
```

- **1 observation now** → the hook works on this machine and **the host did not
  run it**. Go to Step 5.
- Still no store → the hook itself fails here. Send the output of every line;
  the three before the hook say whether git, the launcher or VS Code is the
  cause.

The manual row stays in the store as session `manual-test`. It is harmless.

## Step 5 — Is Chat allowed to run hooks?

1. Settings (**Ctrl+,**), search `chat.useHooks`.
2. Search `hooks` and note any other chat hook settings.
3. Check the status bar for **Restricted Mode**.

- `chat.useHooks` **unticked and "Managed by organization"** → an organisation
  policy has turned Chat hooks off. Nothing in the workspace can turn them
  back on, and Chat will capture nothing through hooks. Go to Step 6.
- Unticked and *not* managed → tick it, reload, and repeat Step 3.
- Restricted Mode → trust the workspace, reload, and repeat Step 3.

## Step 6 — Does Copilot Chat keep its own conversation log?

```
ls -d ~/.vscode-server/data/User/workspaceStorage/*/GitHub.copilot-chat/* 2>/dev/null
find ~/.vscode-server -path '*GitHub.copilot-chat*' -name '*.jsonl' -newermt '-2 hours' 2>/dev/null | head
```

On a desktop (not Remote SSH) install the root is `~/.config/Code/User`
(Linux), `~/Library/Application Support/Code/User` (macOS) or
`%APPDATA%\Code\User` (Windows) instead of `~/.vscode-server/data/User`.

**Expect** a `transcripts/` directory and a recent `transcripts/<session>.jsonl`.

## Step 7 — What is in that log?

Prints each line's type and fields, every value cut to 80 characters:

```
python3 - <<'EOF'
import json, glob, os
f = max(glob.glob(os.path.expanduser('~/.vscode-server/data/User/workspaceStorage/*/GitHub.copilot-chat/transcripts/*.jsonl')), key=os.path.getmtime)
print(f)
def short(v):
    s = json.dumps(v)
    return s[:80] + ('…' if len(s) > 80 else '')
for i, line in enumerate(open(f)):
    d = json.loads(line)
    print(i, d.get('type'), {k: short(v) for k, v in d.items()})
EOF
```

**Expect** `session.start`, then `user.message` / `assistant.turn_start` /
`assistant.message` / `assistant.turn_end` per exchange.

## Step 8 — The log fallback (0.3.0 and later)

When hooks are blocked, the extension reads Copilot's conversation log instead
(setting `cartograph.readCopilotLogs`, on by default) and says so once:

> *Cartograph: Copilot Chat hooks are turned off … Memory is being kept from
> Copilot's own conversation log instead.*

```
carto mem sync --format text
carto mem status --format text
```

**Expect** `mem sync` to list `copilot-chat` with `imported` above 0 the first
time and 0 after, and `mem status` to show a line like

```
capture copilot chat   logs (2 imported, 0 already recorded, at …)
```

`logs` means prompts reached memory only through the log — hooks are blocked
or not firing. `hooks` means every logged prompt had already been recorded by a
hook. The status bar tooltip carries the same line.

## Step 9 — Remote windows: Cartograph Local (0.4.0 and later)

Only for Remote SSH, Dev Containers or WSL, and only matters when Chat hooks
are off. VS Code keeps Chat history on your **local** machine there, and the
engine is on the remote; Cartograph Local bridges the two.

Check where the chat history is. On the **local** machine (Windows PowerShell):

```powershell
Get-ChildItem "$env:APPDATA\Code\User\workspaceStorage\*\workspace.json" |
  Where-Object { Select-String -Path $_.FullName -Pattern "<your repo folder name>" -Quiet } |
  ForEach-Object { $_.DirectoryName; Get-ChildItem (Join-Path $_.DirectoryName "chatSessions") -ErrorAction SilentlyContinue }
```

On the remote, `ls ~/.vscode-server/data/User/workspaceStorage/*/chatSessions`
prints nothing — that is the split.

1. Install `cartograph-local-<version>.vsix` **on the local machine** — accept
   **Install Cartograph Local** in the *hooks are off* notice, run
   **Cartograph: Install Cartograph Local** from the palette, or in local
   PowerShell: `code --install-extension cartograph-local-<version>.vsix`.
   Check with `code --list-extensions --show-versions | Select-String carto`
   in local PowerShell: expect `cartograph.cartograph-local@<version>`, and no
   `cartograph.cartograph` — the main extension belongs on the remote.
2. Reload the window. In the Extensions view, *Cartograph Local* should be
   listed under **Local – Installed**, and *Cartograph* under the remote.
3. Send a Chat prompt, wait about a minute, then in the remote terminal:
   ```
   ls .cartograph/chatSessions/
   carto mem status --format text
   ```
   **Expect** one `.jsonl` per chat, and `capture copilot chat   logs (…)`.

## Copilot CLI

Independent of Chat — run it even when Chat hooks are blocked.

1. In the repository, run `copilot` **interactively** and accept the *trust this
   folder* prompt. The CLI runs repository hooks only in a trusted folder, and
   `copilot -p` in an untrusted one runs none, silently.
2. Send two prompts, then `/exit`.
3. Within ~30 seconds:
   ```
   carto mem search --query "<a word you used>" --doc-type prompts --format text
   carto mem search --query "<same word>" --doc-type sessions --format text
   ```

**Expect** both prompts as `copilot-cli`, and one `sessions` row. If the
folder was not trusted, or CLI hooks are disabled, `carto mem sync` imports the
prompts from `~/.copilot/session-state/` instead, and its `copilot-cli` entry
reports `hooks_fired: false`. Its
`summary_source` should be `host-agent`; `structural` means no `copilot` binary
was reachable from a background process, so the summary is an index of the
prompts rather than a synthesis.

## Findings

### 2026-09-29 — Linux x64, VS Code 1.138.0 over Remote SSH

| Step | Result |
|---|---|
| 1–2 | Installed, hook file placed, `mem summarise` present |
| 3 | **Nothing captured** from Chat |
| 4 | Hook by hand **worked** — store created, row recorded |
| 5 | **`chat.useHooks` off, "Managed by organization"** — the cause |
| 6 | `transcripts/<session>.jsonl` **is** written with hooks off |
| 7 | Readable, and holds the replies — but the first `user.message` of the session was **missing**, and the log ended at `turn_start` before the second reply. Not yet known whether that is delayed writing or the chat's *Checkpoint Restored* state |
| CLI | Not yet run |
| 0.4.0 + Cartograph Local | Installed by hand on Windows (an old local Cartograph 0.1.0 was removed first). The chat file reached `.cartograph/chatSessions/` on the VM; **both Chat prompts recorded as `copilot-chat`**, including the one missing from the VM transcript. The CLI prompt was recorded by its hook (`capture copilot cli: hooks`). Found: the install offer was a separate notification nobody saw; `mem status` said `hooks` for prompts the log had supplied; the VM's own transcript was never read — all fixed in 0.4.1 |
| 0.4.1 | Both extensions removed and reinstalled. The *hooks are off* notice offered **Install Cartograph Local**, and installing from it worked |
| 0.8.3–0.8.4, 2026-09-30/10-01 | **Settling by last message proven**: a new Local chat (JSON lines vs plain text), Sync Memory at once → "No session summarised yet: its last message was at 23:09 … (from 23:39)"; after 23:39 the summary came back `vscode-lm:auto host-agent`, DECIDED JSON lines, DEAD ENDS plain text, PROPOSED none. **0.8.4: `stderr lines: 0`** for a hybrid search on the Hyper-V VM — the ONNX Runtime device-discovery warning, which 0.8.3's logger setting did not stop, is gone |
| 0.8.2, 2026-09-30 | **Summaries through VS Code's models proven.** Remote SSH, Chat hooks off by policy, `summaryHost: vscode`, a Local chat (YAML vs TOML) mirrored by Cartograph Local. The Cartograph log showed `models offered` including id `auto`, `model chosen: auto`, `canSendRequest(auto): true`, `session b56b2303: host-agent via vscode-lm:auto`. The summary: DECIDED TOML (the person's own call), DEAD ENDS YAML (indentation broke configs), PROPOSED "YAML for complex hierarchical data" (the assistant's, unconfirmed). Found: the chat only counted as finished 30 min after the *reload* at 22:17, not the last message at 22:07 — the settle rule used file mtime; fixed next. Also: on 0.8.1 meaning-based search ran on this Ubuntu 22.04 (`mode: hybrid`), with one ONNX Runtime device-discovery warning on stderr per search (Hyper-V PCI path) — fixed in `946d4b6` |
| 0.8.0, 2026-09-30 | A chat started with **New Copilot CLI Session** in the Chat view runs on the Copilot CLI: its prompts are captured by CLI hooks as `copilot-cli` and it is summarised by the CLI, not by VS Code's models. Use **New Chat** (bar shows *Local*) to test the Chat path |
| Remote split | On the **Windows host**: `%APPDATA%\Code\User\workspaceStorage\21bfca…\chatSessions\079499f9….jsonl` (30 KB, today); on the VM: none. Same session id, same workspace id both sides |

**Consequence:** where an organisation disables Chat hooks by policy, hooks
cannot deliver Chat memory, and 0.3.0 adds the log fallback (Step 8).

### 2026-09-29 — macOS, VS Code 1.139.1, `chat.useHooks` off in a test profile

- Copilot's `transcripts/<session>.jsonl` held **only `session.start`** — no
  prompt, no reply. So the transcript alone is not a dependable fallback.
- VS Code's own `chatSessions/<session>.jsonl` held **every prompt and reply**,
  same session id. 0.3.0 reads both.
- End to end through the 0.3.0 extension: a Chat prompt reached memory **75 s**
  after it was sent, `capture copilot chat: logs`.
- Not verified: the one-time notice's wording on screen.
