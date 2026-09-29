/**
 * Cartograph's VS Code extension — an installer that happens to have a status bar.
 *
 * The acceptance test is a fresh machine carrying nothing but VS Code, behind
 * default-deny egress. Copilot ships inside VS Code, so the `.vsix` is the only
 * artifact that reaches such a machine — and installing it therefore has to
 * serve Claude Code and Copilot CLI too, neither of which VS Code knows about.
 * That is the whole job here: place the engine where a shell can find it, place
 * the skills pack where the hosts look, and otherwise stay quiet.
 *
 * It registers no language-model tool and no MCP server. The hosts reach the
 * engine the way they reach any other program: by running it.
 */

import * as vscode from "vscode";
import { Carto, GraphState } from "./carto";
import { Companion, MemorySync, noticeHooksState } from "./memory";
import { cartoHome, placeLaunchers, readPayload } from "./payload";

/** Bumped whenever activation must redo work it would otherwise skip. */
const PLACEMENT_KEY = "cartograph.placedFor";

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const payload = readPayload(context.extensionPath);
  if (typeof payload === "string") {
    // Without a payload there is no engine, and every command below would fail
    // one at a time. Say it once instead.
    vscode.window.showErrorMessage(`Cartograph: ${payload}`);
    return;
  }

  const config = vscode.workspace.getConfiguration("cartograph");
  const home = cartoHome(config.get<string>("home", ""));
  const carto = new Carto(payload);

  let bin: string;
  try {
    bin = placeLaunchers(context.extensionPath, payload, home);
  } catch (err) {
    vscode.window.showErrorMessage(`Cartograph: could not place the carto launcher: ${err}`);
    return;
  }

  // Every integrated terminal gets `carto` on PATH — which is where Copilot's
  // terminal tool, Copilot CLI and Claude Code all run. Persistent and scoped
  // to this extension, so nothing edits the user's shell profile.
  context.environmentVariableCollection.description = "Adds carto to PATH";
  context.environmentVariableCollection.prepend("PATH", `${bin}${pathSeparator()}`);

  const status = new StatusBar(config.get<boolean>("statusBar", true));
  context.subscriptions.push(status);

  context.subscriptions.push(
    vscode.commands.registerCommand("cartograph.build", () =>
      runGraphCommand(carto, status, "build", "Building the graph")),
    vscode.commands.registerCommand("cartograph.update", () =>
      runGraphCommand(carto, status, "update", "Updating the graph")),
    vscode.commands.registerCommand("cartograph.status", () => showStatus(carto)),
    vscode.commands.registerCommand("cartograph.installSkills", () =>
      installIntoWorkspace(carto, context, { force: true })),
    vscode.commands.registerCommand("cartograph.syncMemory", () => syncMemoryNow()),
  );

  // Memory from Copilot's own logs, for whatever the hooks did not record.
  // Started and stopped with the setting, so turning it off takes effect now.
  let memory: MemorySync | undefined;
  const cwd = workspaceRoot();
  const startMemory = () => {
    if (!memory && cwd) {
      memory = new MemorySync(carto, context, cwd, showMemory);
      memory.start();
    }
  };
  const stopMemory = () => {
    memory?.dispose();
    memory = undefined;
    showMemory("memory: reading Copilot's logs is off");
  };
  const syncMemoryNow = async () => {
    if (!memory) {
      vscode.window.showWarningMessage(
        "Cartograph: reading Copilot's logs is off (cartograph.readCopilotLogs).",
      );
      return;
    }
    const result = await memory.syncNow();
    vscode.window.showInformationMessage(
      `Cartograph: ${result?.summary ?? "sync did not complete"} ${memory.describe()}`,
    );
  };
  // Declared before memory starts, so the status line can say when Chat in a
  // remote window is waiting on Cartograph Local.
  let lastMemoryLine = "";
  const showMemory = (text: string) => {
    lastMemoryLine = text;
    status.setMemory(
      companion.needed() ? `${text} · Chat: install Cartograph Local (see notification)` : text,
      companion.needed(),
    );
  };
  const companion: Companion = new Companion(
    context, cwd, () => memory, () => showMemory(lastMemoryLine),
  );
  const readLogs = () =>
    vscode.workspace.getConfiguration("cartograph").get<boolean>("readCopilotLogs", true);
  context.subscriptions.push(
    { dispose: () => memory?.dispose() },
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration("cartograph.readCopilotLogs")) {
        readLogs() ? startMemory() : stopMemory();
      }
    }),
  );

  await checkVersionSkew(carto, payload.engineVersion, context);
  if (config.get<boolean>("installIntoWorkspace", true)) {
    await installIntoWorkspace(carto, context, { force: false });
  }
  await status.refresh(carto);
  if (readLogs()) {
    startMemory();
  } else {
    stopMemory();
  }
  // Not awaited: the notice waits for a companion's hello and then for the
  // person, and activation has nothing that should wait on either.
  void noticeHooksState(context, readLogs(), companion);
}

export function deactivate(): void {
  // The launchers and the skills pack are deliberately left in place: Claude
  // Code and Copilot CLI keep working whether or not VS Code is running, which
  // is the point of installing through it.
}

function pathSeparator(): string {
  return process.platform === "win32" ? ";" : ":";
}

function workspaceRoot(): string | undefined {
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

/**
 * Warn, never block, when the binary and the extension disagree.
 *
 * The hosts talk to the engine as text in and JSON out, which survives most
 * version deltas. A hard block would turn a cosmetic mismatch into an outage.
 */
async function checkVersionSkew(
  carto: Carto,
  expected: string,
  context: vscode.ExtensionContext,
): Promise<void> {
  const cwd = workspaceRoot() ?? context.extensionPath;
  let reported: string;
  try {
    reported = await carto.version(cwd);
  } catch (err) {
    vscode.window.showErrorMessage(`Cartograph: the bundled engine did not run: ${err}`);
    return;
  }
  if (!reported.includes(expected)) {
    vscode.window.showWarningMessage(
      `Cartograph: bundled engine reports "${reported}", but this extension was ` +
        `packaged against ${expected}. Reinstalling the .vsix is the fix.`,
    );
  }
}

/**
 * Put the skills pack in the three directories the hosts read.
 *
 * `carto install` already does this, including the byte-for-byte guarantee
 * that the installed pack matches the canonical one, so it is called rather
 * than reimplemented. Once per workspace per extension version unless forced:
 * re-running is harmless but not free, and activation should not cost a
 * subprocess on every window.
 */
async function installIntoWorkspace(
  carto: Carto,
  context: vscode.ExtensionContext,
  opts: { force: boolean },
): Promise<void> {
  const cwd = workspaceRoot();
  if (!cwd) {
    if (opts.force) {
      vscode.window.showWarningMessage("Cartograph: open a folder first.");
    }
    return;
  }
  const stamp = `${context.extension.packageJSON.version}:${cwd}`;
  if (!opts.force && context.workspaceState.get<string>(PLACEMENT_KEY) === stamp) {
    return;
  }
  const result = await carto.installIntoWorkspace(cwd);
  if (!result.ok) {
    vscode.window.showErrorMessage(`Cartograph: carto install failed.\n${result.output}`);
    return;
  }
  await context.workspaceState.update(PLACEMENT_KEY, stamp);
  if (opts.force) {
    vscode.window.showInformationMessage(
      "Cartograph: skills and hooks installed for Copilot Chat and Copilot CLI.",
    );
  }
}

async function runGraphCommand(
  carto: Carto,
  status: StatusBar,
  command: "build" | "update",
  title: string,
): Promise<void> {
  const cwd = workspaceRoot();
  if (!cwd) {
    vscode.window.showWarningMessage("Cartograph: open a folder first.");
    return;
  }
  // A terminal, not a progress spinner: a build takes minutes and prints its
  // progress, which is exactly what someone who asked for it wants to watch.
  // It also runs through the launcher on PATH, so a failure here is a failure
  // of the same path the other two hosts use.
  const terminal = vscode.window.createTerminal({ name: `Cartograph: ${title}`, cwd });
  terminal.sendText(`carto ${command} --repo "${cwd}"`);
  terminal.show();
  await status.refresh(carto);
}

async function showStatus(carto: Carto): Promise<void> {
  const cwd = workspaceRoot();
  if (!cwd) {
    vscode.window.showWarningMessage("Cartograph: open a folder first.");
    return;
  }
  const state = await carto.graphState(cwd);
  vscode.window.showInformationMessage(`Cartograph: ${describe(state)}`);
}

function describe(state: GraphState): string {
  switch (state.kind) {
    case "absent":
      return `no graph yet — ${state.remediation}`;
    case "stale":
      return `graph is stale (${state.status.files} files, built on another branch) — run Update`;
    case "ready":
      return `graph ready: ${state.status.nodes} nodes, ${state.status.edges} edges, ` +
        `${state.status.files} files, ${state.status.languages.length} languages`;
    case "broken":
      return `could not read graph state — ${state.detail}`;
  }
}

/**
 * The only always-on surface.
 *
 * A notification on every window would be noise: an absent or stale graph is a
 * normal state, not an incident. The status bar says which it is and offers the
 * one command that changes it.
 */
class StatusBar implements vscode.Disposable {
  private readonly item: vscode.StatusBarItem;
  private graph = "";
  private memory = "";

  constructor(private readonly enabled: boolean) {
    this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  }

  async refresh(carto: Carto): Promise<void> {
    if (!this.enabled) {
      return;
    }
    const cwd = workspaceRoot();
    if (!cwd) {
      this.item.hide();
      return;
    }
    const state = await carto.graphState(cwd);
    this.base = {
      absent: "$(circle-outline) carto",
      stale: "$(warning) carto stale",
      ready: "$(circle-filled) carto",
      broken: "$(error) carto",
    }[state.kind];
    this.render();
    this.graph = describe(state);
    this.item.tooltip = this.tooltip();
    this.item.command = state.kind === "absent" ? "cartograph.build" : "cartograph.update";
    this.item.show();
  }

  private attention = false;
  private base = "";

  /** The memory line, from the last `carto mem sync`, and whether it needs acting on. */
  setMemory(text: string, attention = false): void {
    this.memory = text;
    this.attention = attention;
    this.item.tooltip = this.tooltip();
    this.render();
  }

  private render(): void {
    if (this.base) {
      this.item.text = this.attention ? `${this.base} $(bell-dot)` : this.base;
    }
  }

  private tooltip(): string {
    return [this.graph, this.memory].filter(Boolean).join("\n");
  }

  dispose(): void {
    this.item.dispose();
  }
}
