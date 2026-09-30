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
import * as log from "./log";
import { Awaiting, describeLatest, summariseArgs, SummaryHost, VsCodeSummaries } from "./summaries";

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

export interface SyncData {
  summary: string;
  hosts: Record<string, HostCapture>;
  /** Present when the engine handed summaries to this extension. */
  awaiting_summary?: Awaiting[];
  /** Present with --summarise when the engine wrote summaries itself. */
  summarised?: unknown[];
}

/** What the store holds, from `mem status`, rather than what one sync read. */
export interface MemoryHeld {
  sessions: number;
  observations: number;
  /** Host name → "hooks" | "logs" | "unknown", from the last sync that saw prompts. */
  capture: Record<string, string>;
}

/**
 * Read `mem status`'s `capture_copilot_chat` / `capture_copilot_cli` lines —
 * "logs (3 imported, 0 already recorded, at …)" — and its counts.
 *
 * These are persisted by `mem sync` from the last run that saw a prompt, so a
 * run that read nothing new does not erase what earlier runs established.
 */
export function readHeld(data: Record<string, unknown>): MemoryHeld {
  const capture: Record<string, string> = {};
  for (const [key, value] of Object.entries(data)) {
    if (key.startsWith("capture_") && typeof value === "string") {
      capture[key.slice("capture_".length).replace(/_/g, "-")] = value.split(" ")[0];
    }
  }
  const count = (value: unknown) => (typeof value === "number" ? value : 0);
  return { sessions: count(data.sessions), observations: count(data.observations), capture };
}

/** The status-bar line for what memory holds. */
export function describeHeld(held: MemoryHeld | undefined): string {
  if (!held) {
    return "memory: not synced yet";
  }
  const parts = Object.entries(held.capture)
    .filter(([, mode]) => mode !== "unknown")
    .map(([name, mode]) => `${name} via ${mode}`);
  if (parts.length) {
    return `memory: ${parts.join(", ")}`;
  }
  // Nothing says how prompts arrive (no sync has seen one yet), but the store
  // may still hold sessions — from hooks, `mem add`, or another window.
  if (held.sessions || held.observations) {
    return `memory: ${held.sessions} session(s), ${held.observations} observation(s)`;
  }
  return "memory: no Copilot sessions yet";
}

/** One log line for a sync run: its flags, what it imported, what it wrote or handed over. */
export function describeSync(data: SyncData, args: string[], userInitiated: boolean): string {
  const hosts = Object.entries(data.hosts ?? {})
    .map(([name, h]) => `${name} ${h.imported} imported/${h.already_recorded} already (${h.capture})`)
    .join(", ");
  const summarised = data.summarised?.length ? ` · ${data.summarised.length} summarised by the engine` : "";
  const awaiting = data.awaiting_summary ? ` · ${data.awaiting_summary.length} handed over` : "";
  return `sync (${userInitiated ? "user-initiated" : "background"}; ${args.join(" ")}): ` +
    `${hosts || "no logs"}${summarised}${awaiting}`;
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
  private held: MemoryHeld | undefined;
  private cost: string[] = [];
  private summaryLine: string | undefined;
  private userInitiated = false;
  private readonly summaries: VsCodeSummaries;

  constructor(
    private readonly carto: Carto,
    private readonly context: vscode.ExtensionContext,
    private readonly cwd: string,
    private readonly onResult: (text: string) => void,
  ) {
    this.summaries = new VsCodeSummaries(
      carto, context, cwd, () => this.logDirArgs(), () => void this.sync(),
    );
  }

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
    // A person asked: the one run where VS Code's consent dialog may appear
    // without Cartograph asking first.
    this.userInitiated = true;
    await this.sync();
    return this.last;
  }

  /** One line for the status bar tooltip. */
  describe(): string {
    const line = describeHeld(this.held);
    return [line, ...(this.summaryLine ? [this.summaryLine] : []), ...this.cost].join("\n");
  }

  /**
   * What memory holds (for `describe`), what it cost and what it replaced,
   * from `carto mem status`: counted
   * Copilot calls, served recall at chars/4, and summaries against the raw
   * sessions they stand for. In the tooltip only — a line added to every
   * session would cost tokens on every session to report on tokens.
   */
  private async costLines(): Promise<string[]> {
    try {
      const status = await this.carto.json<Record<string, unknown>>(
        ["mem", "status", "--repo", this.cwd], this.cwd,
      );
      if (!status.ok) {
        return [];
      }
      this.held = readHeld(status.data);
      const latest = status.data.latest_summary_by;
      this.summaryLine = describeLatest(
        typeof latest === "string" ? latest : undefined, this.summaries.why,
      );
      return ["memory_cost", "vs_raw_logs"]
        .map((key) => status.data[key])
        .filter((value): value is string => typeof value === "string");
    } catch {
      return [];
    }
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

  /** Where the engine reads Chat logs, for sync and for the replies in a brief. */
  private logDirArgs(): string[] {
    const workspace = this.workspaceStorageDir();
    return [
      "--vscode-user-dir", this.userDir(),
      // Named, because on a remote this directory has Copilot's transcripts
      // but no workspace.json for the engine to match on.
      ...(workspace ? ["--vscode-workspace-dir", workspace] : []),
    ];
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
          const host = vscode.workspace
            .getConfiguration("cartograph")
            .get<SummaryHost>("summaryHost", "auto");
          const userInitiated = this.userInitiated;
          this.userInitiated = false;
          const args = summariseArgs(host, this.summaries.ruledOutReason);
          const envelope = await this.carto.json<SyncData>(
            ["mem", "sync", ...args, ...this.logDirArgs(), "--repo", this.cwd],
            this.cwd,
          );
          if (envelope.ok) {
            this.last = envelope.data;
            log.info(describeSync(envelope.data, args, userInitiated));
            // Its failure must not cost the status line; the sessions wait for
            // the next run, since nothing was written for them.
            await this.summaries
              .run(envelope.data.awaiting_summary ?? [], userInitiated)
              .catch((err) => log.warn(`summaries: run failed, nothing written: ${err}`));
            this.cost = await this.costLines();
            this.onResult(this.describe());
          } else {
            log.warn(`sync: ${envelope.error?.message ?? "failed without a message"}`);
          }
        } catch (err) {
          // A failed sync leaves memory as it was, and the next write or the
          // timer tries again. Nothing a notification could ask anyone to do,
          // but the log keeps it.
          log.warn(`sync: ${String(err).slice(0, 300)}`);
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
 * In a remote window that is also where Cartograph Local is offered: one
 * notice, not two. On the Remote SSH VM the offer was a second notification 90
 * seconds after the first, and it was never seen. Shown again only when the
 * situation changes, and the status bar keeps saying so in the meantime.
 */
export async function noticeHooksState(
  context: vscode.ExtensionContext,
  readLogs: boolean,
  companion: Companion,
): Promise<void> {
  const hooks = chatHooksState();
  if (hooks.enabled) {
    return;
  }
  const remote = Boolean(vscode.env.remoteName);
  const needsCompanion = remote && readLogs && !(await companion.waitForHello(HELLO_WAIT_MS));
  const situation = !readLogs ? "hooks-off-nothing"
    : needsCompanion ? "hooks-off-needs-companion"
    : "hooks-off-logs";
  const version = context.extension.packageJSON.version as string;
  if (context.globalState.get<string>(NOTICE_KEY) === `${situation}@${version}`) {
    return;
  }
  await context.globalState.update(NOTICE_KEY, `${situation}@${version}`);
  const cause = hooks.setByPolicy
    ? "Copilot Chat hooks are turned off, and not by a user or workspace setting — most likely an organization policy."
    : "Copilot Chat hooks are turned off (chat.useHooks).";

  if (!readLogs) {
    const choice = await vscode.window.showWarningMessage(
      `Cartograph: ${cause} Reading Copilot's conversation log is also off, so Chat sessions are not being remembered.`,
      "Turn on",
    );
    if (choice === "Turn on") {
      await vscode.workspace
        .getConfiguration("cartograph")
        .update("readCopilotLogs", true, vscode.ConfigurationTarget.Global);
    }
    return;
  }
  if (needsCompanion) {
    const choice = await vscode.window.showWarningMessage(
      `Cartograph: ${cause} This is a remote window, and VS Code keeps Chat history on ` +
        "your local machine, where Cartograph cannot read it. Install Cartograph Local " +
        "there so Chat sessions are remembered.",
      "Install Cartograph Local",
      "How",
    );
    if (choice === "Install Cartograph Local") {
      await companion.install();
    } else if (choice === "How") {
      await companion.explain();
    }
    return;
  }
  const choice = await vscode.window.showInformationMessage(
    `Cartograph: ${cause} Memory is being kept from Copilot's own conversation log instead.`,
    "Settings",
  );
  if (choice === "Settings") {
    void vscode.commands.executeCommand(
      "workbench.action.openSettings", "cartograph.readCopilotLogs",
    );
  }
}

/**
 * How long activation waits for an installed companion to say hello before
 * treating it as missing. It polls every 15 s, and says hello first.
 */
const HELLO_WAIT_MS = 20_000;

/**
 * The remote half of Cartograph Local.
 *
 * In a remote window VS Code keeps Chat history on the local machine, where
 * the engine cannot read it. The companion extension runs there, and sends
 * each changed chat file here through `cartograph.receiveChatSession`; this
 * writes it into `.cartograph/chatSessions/` in the repository — beside the
 * memory store, already out of git — where `carto mem sync` reads it with the
 * same parser as a local window's. Measured working on the Remote SSH VM.
 */
export class Companion {
  private heard = false;
  private waiters: Array<(heard: boolean) => void> = [];

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly cwd: string | undefined,
    private readonly memory: () => MemorySync | undefined,
    private readonly onChange: () => void,
  ) {
    context.subscriptions.push(
      vscode.commands.registerCommand("cartograph.receiveChatSession", (message) =>
        this.receive(message)),
      vscode.commands.registerCommand("cartograph.companionHello", () => {
        this.heard = true;
        this.waiters.splice(0).forEach((resolve) => resolve(true));
        this.onChange();
        return true;
      }),
      vscode.commands.registerCommand("cartograph.installCompanion", () => this.install()),
    );
  }

  /** A remote window with Chat hooks off, and no companion heard from. */
  needed(): boolean {
    return Boolean(vscode.env.remoteName) && !chatHooksState().enabled && !this.heard;
  }

  /** True as soon as a companion says hello, false if none does within *ms*. */
  waitForHello(ms: number): Promise<boolean> {
    if (this.heard) {
      return Promise.resolve(true);
    }
    return new Promise((resolve) => {
      this.waiters.push(resolve);
      setTimeout(() => resolve(this.heard), ms);
    });
  }

  /**
   * Install the companion this `.vsix` carries.
   *
   * An attempt, not a promise: the file is on the remote and the companion
   * must land on the local machine, and whether VS Code bridges that for an
   * extension's install call has not been observed yet. When it does not, the
   * person is told exactly what to install and from where.
   */
  async install(): Promise<void> {
    const vsix = vscode.Uri.file(
      path.join(this.context.extensionPath, "companion", "cartograph-local.vsix"),
    );
    try {
      await vscode.commands.executeCommand("workbench.extensions.installExtension", vsix);
      vscode.window.showInformationMessage(
        "Cartograph: Cartograph Local installed. Reload the window to start it.",
      );
    } catch {
      await this.explain();
    }
  }

  async explain(): Promise<void> {
    const version = this.context.extension.packageJSON.version as string;
    await vscode.window.showInformationMessage(
      `Cartograph: download cartograph-local-${version}.vsix from the same release as this ` +
        "extension and install it on your LOCAL machine — in PowerShell or a local terminal: " +
        `code --install-extension cartograph-local-${version}.vsix — then reload this window.`,
    );
  }

  private receive(message: { sessionId?: unknown; content?: unknown } | undefined): boolean {
    const { sessionId, content } = message ?? {};
    // The id becomes a file name, so it is held to the shape VS Code gives it;
    // anything else is refused rather than cleaned up.
    if (!this.cwd || typeof sessionId !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(sessionId)
      || typeof content !== "string") {
      return false;
    }
    const dir = path.join(this.cwd, ".cartograph", "chatSessions");
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, `${sessionId}.jsonl`), content, "utf8");
    this.memory()?.nudge();
    return true;
  }
}
