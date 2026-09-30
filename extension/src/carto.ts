/**
 * Running the engine and reading what it says back.
 *
 * Every command speaks the capability envelope under `--format json`, and in
 * that mode stdout carries nothing but the envelope — logs go to stderr. So
 * parsing is unconditional: a stdout that does not parse is a bug worth
 * surfacing, not a case to tolerate.
 *
 * Exit codes are the query protocol's, not the shell's: 0 success (an empty
 * result is success), 1 usage, 2 precondition with an actionable
 * `error.remediation`, 3 internal.
 */

import * as cp from "child_process";
import { Payload } from "./payload";

export interface Envelope<T = unknown> {
  schema: number;
  ok: boolean;
  tool: string;
  data: T;
  error?: { code: string; message: string; remediation?: string };
}

export interface StatusData {
  nodes: number;
  edges: number;
  files: number;
  languages: string[];
  stale: boolean;
}

/** `absent` when there is no graph, otherwise what `status` reported. */
export type GraphState =
  | { kind: "absent"; remediation: string }
  | { kind: "ready"; status: StatusData }
  | { kind: "stale"; status: StatusData }
  | { kind: "broken"; detail: string };

export class Carto {
  constructor(private readonly payload: Payload) {}

  /**
   * The environment the engine needs, on top of the caller's.
   *
   * The grammar variable is the difference between working and not on a
   * default-deny machine: the grammar pack downloads parser libraries on first
   * use unless pointed at a seeded cache, and the `.vsix` carries one. The
   * model variable is what makes memory search meaning-based rather than
   * keyword-only. Both match what the launchers set.
   */
  private env(): NodeJS.ProcessEnv {
    return {
      ...process.env,
      TREE_SITTER_LANGUAGE_PACK_CACHE_DIR:
        process.env.TREE_SITTER_LANGUAGE_PACK_CACHE_DIR ?? this.payload.grammars,
      CARTO_EMBEDDING_MODEL_DIR:
        process.env.CARTO_EMBEDDING_MODEL_DIR ?? this.payload.model,
    };
  }

  /** Run a command and hand back its envelope, whatever the exit code says. */
  async json<T>(args: string[], cwd: string): Promise<Envelope<T>> {
    const { stdout, code } = await this.run([...args, "--format", "json"], cwd);
    try {
      return JSON.parse(stdout) as Envelope<T>;
    } catch {
      throw new Error(
        `carto ${args.join(" ")} exited ${code} without an envelope on stdout: ` +
          `${stdout.slice(0, 400)}`,
      );
    }
  }

  /** `carto --version`, which is how the binary identifies itself. */
  async version(cwd: string): Promise<string> {
    const { stdout } = await this.run(["--version"], cwd);
    return stdout.trim();
  }

  /**
   * Place the skills pack and hooks in this workspace.
   *
   * `--no-instructions` because the instruction file is a tracked project
   * file. An extension that edits it on activation hands the user a dirty
   * worktree they did not ask for; the skills pack is the mechanism both
   * Copilot hosts actually read.
   *
   * There is no MCP server, by design.
   */
  async installIntoWorkspace(cwd: string): Promise<{ ok: boolean; output: string }> {
    const { stdout, stderr, code } = await this.run(
      // `copilot` covers both Copilot CLI and Copilot Chat: one hook file in
      // `.github/hooks` and one skills directory, `.github/skills`, which
      // both read.
      ["install", "--platform", "copilot", "--no-instructions", "-y", "--repo", cwd],
      cwd,
    );
    return { ok: code === 0, output: code === 0 ? stdout : `${stdout}\n${stderr}` };
  }

  /** Graph state, in the three shapes the UI needs to distinguish. */
  async graphState(cwd: string): Promise<GraphState> {
    let envelope: Envelope<StatusData>;
    try {
      envelope = await this.json<StatusData>(["status", "--repo", cwd], cwd);
    } catch (err) {
      return { kind: "broken", detail: String(err) };
    }
    if (!envelope.ok) {
      // Exit 2 is a precondition, and the contract guarantees it names the
      // command that fixes it. Passing that string through is better than
      // inventing advice here.
      return {
        kind: "absent",
        remediation: envelope.error?.remediation ?? "run `carto build`",
      };
    }
    return envelope.data.stale
      ? { kind: "stale", status: envelope.data }
      : { kind: "ready", status: envelope.data };
  }

  private run(
    args: string[],
    cwd: string,
  ): Promise<{ stdout: string; stderr: string; code: number }> {
    return new Promise((resolve, reject) => {
      const child = cp.execFile(
        this.payload.executable,
        args,
        { cwd, env: this.env(), maxBuffer: 64 * 1024 * 1024 },
        (err, stdout, stderr) => {
          // execFile reports a non-zero exit as an error. Here a non-zero exit
          // is a documented outcome carrying an envelope, so only a failure to
          // start the process is actually an error.
          const code = typeof err?.code === "number" ? err.code : err ? -1 : 0;
          if (err && typeof err.code !== "number") {
            reject(err);
            return;
          }
          resolve({ stdout, stderr, code });
        },
      );
      child.on("error", reject);
    });
  }
}
