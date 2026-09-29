/**
 * Keeping memory current when Copilot's hooks do not run.
 *
 * Hooks are how prompts reach memory, and an organisation can switch them off:
 * on the first Linux test machine `chat.useHooks` was disabled by policy, the
 * hook worked when run by hand, and Chat captured nothing without a word.
 * Copilot writes its own conversation log regardless, and `carto mem sync`
 * imports what the hooks missed. This file runs that import and says, once,
 * that it is doing so — a fallback nobody can see is how the failure went
 * unnoticed in the first place.
 *
 * Sync is a reconciliation: a prompt a hook already recorded is not recorded
 * again, so running it while hooks work costs a directory scan and changes
 * nothing. That is why it runs whether or not hooks are blocked — hooks can
 * also stop firing for reasons no setting shows (workspace trust, a Preview
 * feature changing), and only the logs reveal that.
 */

import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import { Carto } from "./carto";

/** Once per machine, and again only if the situation it describes changes. */
const NOTICE_KEY = "cartograph.hooksNoticeShown";

/** After a burst of transcript writes, wait for the chat to settle. */
const DEBOUNCE_MS = 30_000;

/**
 * Also on a timer, because `--summarise` waits for a session's log to go
 * quiet, and a quiet log is exactly the one no watcher will fire for again.
 */
const INTERVAL_MS = 10 * 60_000;

export interface HostCapture {
  capture: "hooks" | "logs" | "unknown";
  imported: number;
  already_recorded: number;
}

interface SyncData {
  summary: string;
  hosts: Record<string, HostCapture>;
}

/** What `chat.useHooks` says, and whether anyone in reach of this UI set it. */
export function chatHooksState(): { enabled: boolean; setByPolicy: boolean } {
  const chat = vscode.workspace.getConfiguration("chat");
  const enabled = chat.get<boolean>("useHooks", true) !== false;
  const inspected = chat.inspect<boolean>("useHooks");
  // Off, and not by the user, the workspace or a folder: the extension API
  // does not expose policy directly, so this is the inference, and the
  // notice words it as one.
  const setByPolicy =
    !enabled &&
    inspected?.globalValue === undefined &&
    inspected?.workspaceValue === undefined &&
    inspected?.workspaceFolderValue === undefined;
  return { enabled, setByPolicy };
}

export class MemorySync implements vscode.Disposable {
  private readonly disposables: vscode.Disposable[] = [];
  private timer: NodeJS.Timeout | undefined;
  private running: Promise<void> | undefined;
  private rerun = false;
  private last: SyncData | undefined;

  constructor(
    private readonly carto: Carto,
    private readonly context: vscode.ExtensionContext,
    private readonly cwd: string,
    private readonly onResult: (text: string) => void,
  ) {}

  /** Start syncing: now, on transcript writes, and on a timer. */
  start(): void {
    const workspace = this.workspaceStorageDir();
    if (workspace) {
      // The parent, not the two log folders: VS Code creates those with the
      // first chat, after activation, and a watcher on a folder that does not
      // exist yet never fires. Measured — a hooks-off chat sat unimported
      // until the timer, because `chatSessions/` appeared a minute after this.
      const watcher = vscode.workspace.createFileSystemWatcher(
        new vscode.RelativePattern(
          vscode.Uri.file(workspace),
          "{chatSessions,GitHub.copilot-chat/transcripts}/*.jsonl",
        ),
      );
      const later = () => this.schedule();
      watcher.onDidCreate(later);
      watcher.onDidChange(later);
      this.disposables.push(watcher);
    }
    const interval = setInterval(() => void this.sync(), INTERVAL_MS);
    this.disposables.push({ dispose: () => clearInterval(interval) });
    void this.sync();
  }

  /** Run once now, for the command palette. */
  async syncNow(): Promise<SyncData | undefined> {
    await this.sync();
    return this.last;
  }

  /** One line for the status bar tooltip. */
  describe(): string {
    if (!this.last) {
      return "memory: not synced yet";
    }
    const parts = Object.entries(this.last.hosts)
      .filter(([, host]) => host.capture !== "unknown")
      .map(([name, host]) => `${name} via ${host.capture}`);
    return parts.length ? `memory: ${parts.join(", ")}` : "memory: no Copilot sessions yet";
  }

  dispose(): void {
    if (this.timer) {
      clearTimeout(this.timer);
    }
    this.disposables.forEach((d) => d.dispose());
  }

  /**
   * `workspaceStorage/<id>` for this workspace, which holds both chat logs:
   * VS Code's own `chatSessions/`, which kept every prompt with hooks off,
   * and Copilot's `GitHub.copilot-chat/transcripts/`, which did not.
   * `storageUri` is `workspaceStorage/<id>/cartograph.cartograph`.
   */
  private workspaceStorageDir(): string | undefined {
    const own = this.context.storageUri?.fsPath;
    return own ? path.dirname(own) : undefined;
  }

  /** `globalStorage/<ext>` → the `User` directory the engine searches. */
  private userDir(): string {
    return path.dirname(path.dirname(this.context.globalStorageUri.fsPath));
  }

  /** Sync soon: something outside this extension changed a chat log. */
  nudge(): void {
    this.schedule();
  }

  private schedule(): void {
    if (this.timer) {
      clearTimeout(this.timer);
    }
    this.timer = setTimeout(() => void this.sync(), DEBOUNCE_MS);
  }

  /** Single-flight: a sync requested mid-run runs once more afterwards. */
  private async sync(): Promise<void> {
    if (this.running) {
      this.rerun = true;
      return this.running;
    }
    this.running = (async () => {
      do {
        this.rerun = false;
        try {
          const envelope = await this.carto.json<SyncData>(
            ["mem", "sync", "--summarise", "--vscode-user-dir", this.userDir(), "--repo", this.cwd],
            this.cwd,
          );
          if (envelope.ok) {
            this.last = envelope.data;
            this.onResult(this.describe());
          }
        } catch {
          // A failed sync leaves memory as it was, and the next write or the
          // timer tries again. Nothing a notification could ask anyone to do.
        }
      } while (this.rerun);
    })();
    try {
      await this.running;
    } finally {
      this.running = undefined;
    }
  }
}

/**
 * Say once, plainly, that Chat hooks are off and what is happening instead.
 *
 * Shown again only if the situation changes — hooks back on, or the fallback
 * switched off — so it is information, not a recurring nag.
 */
export async function noticeHooksState(
  context: vscode.ExtensionContext,
  readLogs: boolean,
): Promise<void> {
  const hooks = chatHooksState();
  const situation = hooks.enabled ? "hooks-on" : readLogs ? "hooks-off-logs" : "hooks-off-nothing";
  if (hooks.enabled || context.globalState.get<string>(NOTICE_KEY) === situation) {
    return;
  }
  await context.globalState.update(NOTICE_KEY, situation);
  const cause = hooks.setByPolicy
    ? "Copilot Chat hooks are turned off, and not by a user or workspace setting — most likely an organization policy."
    : "Copilot Chat hooks are turned off (chat.useHooks).";
  if (readLogs) {
    const choice = await vscode.window.showInformationMessage(
      `Cartograph: ${cause} Memory is being kept from Copilot's own conversation log instead.`,
      "Settings",
    );
    if (choice === "Settings") {
      void vscode.commands.executeCommand(
        "workbench.action.openSettings", "cartograph.readCopilotLogs",
      );
    }
  } else {
    const choice = await vscode.window.showWarningMessage(
      `Cartograph: ${cause} Reading Copilot's conversation log is also off, so Chat sessions are not being remembered.`,
      "Turn on",
    );
    if (choice === "Turn on") {
      await vscode.workspace
        .getConfiguration("cartograph")
        .update("readCopilotLogs", true, vscode.ConfigurationTarget.Global);
    }
  }
}

/** The companion's install offer, once per machine per extension version. */
const COMPANION_OFFER_KEY = "cartograph.companionOffered";

/** How long a remote window waits to hear from a companion before offering one. */
const COMPANION_GRACE_MS = 90_000;

/**
 * The remote half of Cartograph Local.
 *
 * In a remote window VS Code keeps Chat history on the local machine, where
 * the engine cannot read it. The companion extension runs there, and sends
 * each changed chat file here through `cartograph.receiveChatSession`; this
 * writes it into `.cartograph/chatSessions/` in the repository — beside the
 * memory store, already out of git — where `carto mem sync` reads it with the
 * same parser as a local window's.
 */
export function registerCompanion(
  context: vscode.ExtensionContext,
  cwd: string | undefined,
  memory: () => MemorySync | undefined,
): void {
  let heard = false;
  context.subscriptions.push(
    vscode.commands.registerCommand(
      "cartograph.receiveChatSession",
      (message: { sessionId?: unknown; content?: unknown }) => {
        const { sessionId, content } = message ?? {};
        // The id becomes a file name, so it is held to the shape VS Code gives
        // it; anything else is refused rather than cleaned up.
        if (!cwd || typeof sessionId !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(sessionId)
          || typeof content !== "string") {
          return false;
        }
        const dir = path.join(cwd, ".cartograph", "chatSessions");
        fs.mkdirSync(dir, { recursive: true });
        fs.writeFileSync(path.join(dir, `${sessionId}.jsonl`), content, "utf8");
        memory()?.nudge();
        return true;
      },
    ),
    vscode.commands.registerCommand("cartograph.companionHello", () => {
      heard = true;
      return true;
    }),
  );

  if (!vscode.env.remoteName) {
    return;
  }
  const timer = setTimeout(() => {
    if (!heard && !chatHooksState().enabled) {
      void offerCompanion(context);
    }
  }, COMPANION_GRACE_MS);
  context.subscriptions.push({ dispose: () => clearTimeout(timer) });
}

/**
 * Ask once to install Cartograph Local, which this `.vsix` carries.
 *
 * Installing it from here is an attempt, not a promise: the file is on the
 * remote and the companion must land on the local machine, and whether VS Code
 * bridges that for an extension's install call is not something this code has
 * been able to observe. When it does not, the person is told exactly what to
 * install and from where.
 */
async function offerCompanion(context: vscode.ExtensionContext): Promise<void> {
  const version = context.extension.packageJSON.version as string;
  if (context.globalState.get<string>(COMPANION_OFFER_KEY) === version) {
    return;
  }
  await context.globalState.update(COMPANION_OFFER_KEY, version);
  const choice = await vscode.window.showWarningMessage(
    "Cartograph: this is a remote window, Copilot Chat hooks are off, and VS Code keeps " +
      "Chat history on your local machine, where Cartograph cannot read it. Install the " +
      "Cartograph Local companion so Chat sessions are remembered.",
    "Install",
    "How",
  );
  const manual = () =>
    vscode.window.showInformationMessage(
      `Cartograph: install cartograph-local-${version}.vsix from the same release as ` +
        "this extension, with 'Extensions: Install from VSIX…' — it installs on your " +
        "local machine. Then reload the window.",
    );
  if (choice === "Install") {
    const vsix = vscode.Uri.file(path.join(context.extensionPath, "companion", "cartograph-local.vsix"));
    try {
      await vscode.commands.executeCommand("workbench.extensions.installExtension", vsix);
      vscode.window.showInformationMessage(
        "Cartograph: Cartograph Local installed. Reload the window to start it.",
      );
    } catch {
      await manual();
    }
  } else if (choice === "How") {
    await manual();
  }
}
