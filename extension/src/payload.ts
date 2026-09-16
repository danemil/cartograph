/**
 * Placing the engine payload so every host can reach it.
 *
 * The payload — a frozen `carto` plus the tree-sitter grammars it would
 * otherwise download — ships inside the `.vsix`, one platform per file. This
 * module does the three things that turn a file inside an extension directory
 * into a binary the other two hosts can run:
 *
 *   1. restores the executable bit, which a `.vsix` is a zip and does not keep;
 *   2. writes `$CARTO_HOME/runtime.path`, the pointer the launcher follows;
 *   3. copies the launcher scripts next to it.
 *
 * Nothing here downloads anything. The payload is complete when the `.vsix` is.
 */

import * as cp from "child_process";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

export interface Payload {
  /** The directory inside the extension that the `.vsix` carries. */
  readonly root: string;
  /** The frozen engine itself. */
  readonly executable: string;
  /** Seeded grammar cache, passed to the engine as an environment variable. */
  readonly grammars: string;
  /** Engine version this payload was built from, for the skew check. */
  readonly engineVersion: string;
}

interface PayloadManifest {
  target: string;
  engine_version: string;
  grammar_pack_version: string;
  executable: string;
}

/** Read the payload the `.vsix` carries, or explain why there is none. */
export function readPayload(extensionPath: string): Payload | string {
  const root = path.join(extensionPath, "payload");
  const manifestPath = path.join(root, "PAYLOAD.json");
  if (!fs.existsSync(manifestPath)) {
    return (
      `no engine payload at ${root}. This build of the extension was packaged ` +
      `without one — install the .vsix for your platform (see scripts/build-vsix.sh).`
    );
  }
  let manifest: PayloadManifest;
  try {
    manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8")) as PayloadManifest;
  } catch (err) {
    return `unreadable payload manifest at ${manifestPath}: ${err}`;
  }
  const executable = path.join(root, manifest.executable);
  if (!fs.existsSync(executable)) {
    return `payload manifest names ${manifest.executable}, which is not in ${root}`;
  }
  return {
    root,
    executable,
    grammars: path.join(root, "grammars"),
    engineVersion: manifest.engine_version,
  };
}

/** `~/.cartograph` unless the user has said otherwise. */
export function cartoHome(configured: string): string {
  const trimmed = configured.trim();
  if (trimmed) {
    return trimmed.startsWith("~")
      ? path.join(os.homedir(), trimmed.slice(1))
      : trimmed;
  }
  return path.join(os.homedir(), ".cartograph");
}

/**
 * Make the payload runnable and point the launchers at it.
 *
 * Idempotent and cheap, so it runs on every activation rather than being
 * remembered: an extension update moves the payload to a new versioned
 * directory, and the pointer has to follow it.
 *
 * Returns the directory to put on PATH.
 */
export function placeLaunchers(
  extensionPath: string,
  payload: Payload,
  home: string,
): string {
  if (process.platform !== "win32") {
    restoreExecutableBits(payload);
  }

  const bin = path.join(home, "bin");
  fs.mkdirSync(bin, { recursive: true });
  fs.writeFileSync(path.join(home, "runtime.path"), payload.root, "utf8");

  const source = path.join(extensionPath, "launcher");
  // Both names: the hook lines `carto install` writes guard on `cartograph`
  // being resolvable and then invoke `carto`. Shipping only one of the two
  // makes every hook a silent no-op.
  const names = process.platform === "win32"
    ? ["carto.cmd", "cartograph.cmd"]
    : ["carto", "cartograph"];
  const template = path.join(source, process.platform === "win32" ? "carto.cmd" : "carto");
  for (const name of names) {
    const dest = path.join(bin, name);
    fs.copyFileSync(template, dest);
    if (process.platform !== "win32") {
      fs.chmodSync(dest, 0o755);
    }
  }
  return bin;
}

/**
 * A `.vsix` is a zip, and VS Code's unpack does not carry the executable bit.
 * PyInstaller's onedir tree needs it on the entry binary and on the loader
 * shims beside it.
 */
function restoreExecutableBits(payload: Payload): void {
  fs.chmodSync(payload.executable, 0o755);
  const internal = path.join(path.dirname(payload.executable), "_internal");
  if (fs.existsSync(internal)) {
    for (const entry of fs.readdirSync(internal, { withFileTypes: true })) {
      if (entry.isFile() && path.extname(entry.name) === "") {
        fs.chmodSync(path.join(internal, entry.name), 0o755);
      }
    }
  }
  if (process.platform === "darwin") {
    clearQuarantine(payload.root);
  }
}

/**
 * macOS marks anything that arrived through a browser download as quarantined,
 * and refuses to exec it. The `.vsix` inherits the mark and passes it to every
 * file it unpacks, so a user who downloaded the release asset gets
 * "killed: 9" from a binary that is otherwise fine.
 *
 * This clears the mark. It does not make the binary signed — an unsigned,
 * un-notarised build still trips Gatekeeper in stricter configurations, and
 * that needs a signing identity, not code.
 */
function clearQuarantine(root: string): void {
  try {
    cp.execFileSync("xattr", ["-d", "-r", "com.apple.quarantine", root], {
      stdio: "ignore",
    });
  } catch {
    // Absent on a file that was never quarantined, which is the common case.
  }
}
