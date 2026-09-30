# Cartograph for VS Code

Installs Cartograph — a code knowledge graph for AI coding agents — and gets
out of the way. **No MCP server**, because the environment this is built for
prohibits them.

## What installing it does

On activation, in order:

1. Restores the executable bit on the bundled `carto` binary, which a `.vsix`
   is a zip and does not carry, and clears the macOS quarantine mark.
2. Writes a `carto` launcher into `~/.cartograph/bin` and puts that directory
   on the PATH of every VS Code terminal.
3. Runs `carto install --platform copilot`, which writes the skills pack into
   `.github/skills` and one hook file, `.github/hooks/cartograph.json`, both
   read by Copilot Chat and Copilot CLI. It lists those files and
   `.cartograph/` in the repository's local `.git/info/exclude`, so they do
   not appear in `git status`; the tracked `.gitignore` is not edited, and
   files the repository already tracks are left as they are.
4. Reads `carto status` and shows the graph state in the status bar.

The hooks are what make memory automatic: every prompt is recorded, each
session is summarised when it ends (or, in Chat, when the next one starts), and
the graph refreshes at the end of every agent turn. See
`docs/copilot-hooks.md` in the monorepo for what each host does and what was
verified.

**When hooks are blocked, memory still works.** An organization can switch
Copilot Chat hooks off by policy (`chat.useHooks`). Copilot writes its own
conversation log either way, and the extension imports from it whatever the
hooks did not record — on activation, after each chat, and every ten minutes.
It says so once when it detects hooks are off, and the status bar tooltip
shows, per host, whether memory is arriving via **hooks** or **logs**. Setting:
`cartograph.readCopilotLogs` (on by default). Command: **Cartograph: Sync
Memory from Copilot Logs**.

**Remote windows (Remote SSH, Dev Containers, WSL).** VS Code keeps Chat
history on your local machine, while this extension and the engine run on the
remote. If Chat hooks are off, install **Cartograph Local**
(`cartograph-local-<version>.vsix`, carried inside this extension and on the
release page) on the local side; it passes Chat history across. This
extension offers to install it when it sees the need.

**Session summaries without Copilot CLI.** Each finished session gets one
summary. Where the engine finds `copilot` on PATH it writes it with
`copilot -p --model auto`. Where it does not — often the case on a machine with
only VS Code — the extension asks Copilot's models in VS Code instead
(`vscode.lm`, Copilot's **Auto** model by default, on your own Copilot plan),
and stores the answer through the engine. VS Code asks once before an
extension may use Copilot's models; Cartograph asks first, in a notification,
rather than raising that dialog from a background run. If permission is
refused, the request is blocked by quota or policy, or the model is not
offered, that session and the rest in this window get a structural summary (a
list of the prompts, no synthesis), and a notification says so once. The status
bar tooltip names what wrote the latest summary.

**Copilot CLI only runs repository hooks in a folder you have trusted.** Start
`copilot` interactively in the repository once and accept the trust prompt.

## What it does not do

- **No network access, ever.** The engine and its tree-sitter grammars are
  inside the `.vsix`. The extension downloads nothing at install time or after.
  A summary written through VS Code's models is a request Copilot makes on
  your behalf, through VS Code, as Copilot Chat's own requests are.
- **No MCP registration.** There is no MCP server to register.
- **No edits to your instruction files.** The extension passes
  `--no-instructions`, so `.github/instructions/cartograph.instructions.md`,
  a tracked project file, is left alone. Run `carto install -y` yourself if
  you want it.

## Commands

| Command | What |
|---|---|
| Cartograph: Build Graph | First build. Runs in a terminal — it takes minutes on a large repo. |
| Cartograph: Update Graph | Incremental refresh after edits. |
| Cartograph: Show Status | Nodes, edges, files, languages, staleness. |
| Cartograph: Install Skills and Hooks into Workspace | Re-run the placement step. |
| Cartograph: Sync Memory from Copilot Logs | Import from the logs now, and summarise finished sessions — the one run allowed to raise VS Code's model-consent dialog directly. |

## Settings

| Setting | Default | What |
|---|---|---|
| `cartograph.home` | `~/.cartograph` | Where the `carto` launcher is written. |
| `cartograph.installIntoWorkspace` | `true` | Place the skills pack on activation. |
| `cartograph.statusBar` | `true` | Show graph state in the status bar. |
| `cartograph.readCopilotLogs` | `true` | Import prompts from Copilot's own logs when hooks did not record them. |
| `cartograph.summaryHost` | `auto` | What writes session summaries: `auto` (Copilot CLI if the engine finds it, else Copilot's models in VS Code, else structural), `vscode` (VS Code's models only), `cli` (Copilot CLI only), `structural` (no model, no quota). |
| `cartograph.summaryModel` | `auto` | The Copilot model VS Code summarises with, by id or family. `auto` is Copilot's Auto model. |

## Platform-specific builds

Each `.vsix` carries one platform's binary (~130MB). Install the one matching
your machine: `carto-darwin-arm64-*.vsix`, `carto-linux-x64-*.vsix`, and so on.

Apache-2.0. See the monorepo's `LICENSE` and `NOTICE`.
