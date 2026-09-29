/**
 * Cartograph Local — the half of Cartograph that has to run where the window is.
 *
 * In a remote window (Remote SSH, Dev Containers, WSL) VS Code runs in two
 * places. The repository, the terminals, Copilot CLI, Copilot Chat's agent and
 * Cartograph's engine are on the remote. VS Code's own chat history —
 * `workspaceStorage/<id>/chatSessions/` — is on this machine. Measured on
 * 2026-09-29: a Windows host driving an Ubuntu VM over Remote SSH had the chat
 * in `%APPDATA%\Code\User\workspaceStorage\<id>\chatSessions\`, and the VM had
 * no `chatSessions` at all.
 *
 * With Chat hooks allowed that does not matter: hooks run on the remote. Where
 * an organisation blocks them, that history is the only complete record of a
 * Chat conversation, and only an extension running here can read it. This one
 * does exactly that and nothing else: it copies each changed chat file to the
 * Cartograph extension on the remote, through a command, and the engine there
 * reads it with the same parser it uses everywhere else.
 *
 * In a local window it does nothing — Cartograph reads `chatSessions/` itself.
 * No network, no MCP, no engine: this is plain JavaScript.
 */

import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

/** The commands the Cartograph extension registers on the remote. */
const RECEIVE = "cartograph.receiveChatSession";
const HELLO = "cartograph.companionHello";

/** How often to look for changed chat files. A file read is cheap; a chat is slow. */
const POLL_MS = 15_000;

/** A chat file larger than this is skipped rather than sent over the wire whole. */
const MAX_BYTES = 20 * 1024 * 1024;

export function activate(context: vscode.ExtensionContext): void {
  if (!vscode.env.remoteName) {
    return;
  }
  // `storageUri` is this machine's `workspaceStorage/<id>/<this extension>`;
  // VS Code's chat history for the same workspace is a sibling of it.
  const own = context.storageUri?.fsPath;
  if (!own) {
    return;
  }
  const chats = path.join(path.dirname(own), "chatSessions");
  const sent = new Map<string, string>();
  let busy = false;

  const pass = async () => {
    if (busy) {
      return;
    }
    busy = true;
    try {
      for (const name of list(chats)) {
        const file = path.join(chats, name);
        const stamp = stampOf(file);
        if (!stamp || sent.get(name) === stamp.key || stamp.size > MAX_BYTES) {
          continue;
        }
        const content = fs.readFileSync(file, "utf8");
        const sessionId = name.replace(/\.jsonl$/, "");
        // Marked as sent only once the remote accepted it, so a Cartograph
        // extension that is not running yet gets the file on a later pass.
        const accepted = await vscode.commands.executeCommand<boolean>(RECEIVE, {
          sessionId,
          content,
        });
        if (accepted) {
          sent.set(name, stamp.key);
        }
      }
    } catch {
      // Cartograph not installed or not active on the remote yet, or a file
      // mid-write. Nothing to tell anyone; the next pass retries.
    } finally {
      busy = false;
    }
  };

  // Lets the remote side know a companion is present, so it does not ask the
  // person to install one. Retried by the passes if the remote is not up yet.
  const hello = async () => {
    try {
      await vscode.commands.executeCommand(HELLO, { version: context.extension.packageJSON.version });
      return true;
    } catch {
      return false;
    }
  };

  let greeted = false;
  const tick = async () => {
    greeted = greeted || (await hello());
    await pass();
  };
  const timer = setInterval(() => void tick(), POLL_MS);
  context.subscriptions.push({ dispose: () => clearInterval(timer) });
  void tick();
}

export function deactivate(): void {
  // Nothing held: the interval is disposed with the extension.
}

function list(dir: string): string[] {
  try {
    return fs.readdirSync(dir).filter((name) => name.endsWith(".jsonl"));
  } catch {
    return [];
  }
}

function stampOf(file: string): { key: string; size: number } | undefined {
  try {
    const stat = fs.statSync(file);
    return { key: `${stat.mtimeMs}:${stat.size}`, size: stat.size };
  } catch {
    return undefined;
  }
}
