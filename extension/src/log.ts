/**
 * The "Cartograph" Output channel.
 *
 * A summary that came out structural on the first Remote SSH test left no
 * trace of why: the decision was made in memory, its one warning may have gone
 * straight to the notification centre, and there was nothing to read after.
 * Every summary decision and sync run is one line here. Session ids and model
 * ids only — never a prompt, a brief or a reply.
 */

import * as vscode from "vscode";

let channel: vscode.LogOutputChannel | undefined;

function output(): vscode.LogOutputChannel {
  // Created on first use, so a window that never syncs has no empty channel.
  channel ??= vscode.window.createOutputChannel("Cartograph", { log: true });
  return channel;
}

export function info(line: string): void {
  output().info(line);
}

export function warn(line: string): void {
  output().warn(line);
}

export function show(): void {
  output().show(true);
}

export function dispose(): void {
  channel?.dispose();
  channel = undefined;
}
