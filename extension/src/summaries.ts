/**
 * Session summaries through VS Code's language models, when no `copilot` CLI
 * is there to write them.
 *
 * The engine summarises a finished session with `copilot -p --model auto`.
 * The machines this targets often have VS Code and nothing else, and every
 * summary there used to fall back to a structural one: a list of prompts, no
 * synthesis. Copilot Chat offers the same models to extensions through
 * `vscode.lm`, on the person's own subscription, so the order is: Copilot CLI,
 * then this, then structural (decision 1 in docs/CONTINUE.md).
 *
 * The engine keeps every rule. `carto mem sync --hand-off` names the sessions
 * awaiting a summary and writes nothing for them; `--brief-only` hands over
 * the brief the CLI would have been sent; `--answer-file` stores the answer
 * through the code path a CLI answer takes, so the one-summary-per-session
 * rule, the labels and the cost counter are the engine's, not a copy here.
 *
 * Every `vscode.lm` call Cartograph makes is in this file.
 */

import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as vscode from "vscode";
import { Carto } from "./carto";

/** `cartograph.summaryHost`. */
export type SummaryHost = "auto" | "vscode" | "cli" | "structural";

export interface Awaiting {
  session: string;
  prompts: number;
}

/**
 * Shown in the consent dialog VS Code raises on the first request. It is the
 * only place the person learns why an extension wants their Copilot models.
 */
const JUSTIFICATION = "Cartograph summarises a finished Copilot session into your project memory.";

/**
 * The engine's own limit for a `copilot` call. A request past it is stopped
 * and that session gets a structural summary, as a CLI timeout would give it.
 */
const TIMEOUT_MS = 90_000;

type Outcome =
  | { ok: true; text: string }
  | { ok: false; reason: string; stop: boolean };

/**
 * The engine's `--summarise` flags for this setting and this window's state.
 *
 * Once VS Code's models have been ruled out for the window, `auto` is the
 * CLI-then-structural path the engine had before, and `vscode` is structural:
 * someone who chose VS Code's models has said not to use the CLI.
 */
export function summariseArgs(host: SummaryHost, vscodeRuledOut: boolean): string[] {
  switch (host) {
    case "structural":
      return ["--summarise", "--no-host-agent"];
    case "cli":
      return ["--summarise"];
    case "vscode":
      return vscodeRuledOut
        ? ["--summarise", "--no-host-agent"]
        : ["--summarise", "--hand-off", "always"];
    default:
      return vscodeRuledOut ? ["--summarise"] : ["--summarise", "--hand-off", "no-cli"];
  }
}

/** The tooltip line for the newest summary, from `mem status`'s `latest_summary_by`. */
export function describeLatest(by: string | undefined, why: string | undefined): string | undefined {
  if (!by) {
    return why ? `summaries: ${why}` : undefined;
  }
  if (by === "copilot-cli") {
    return "summaries via Copilot CLI";
  }
  if (by.startsWith("vscode-lm:")) {
    return `summaries via VS Code (model ${by.slice("vscode-lm:".length)})`;
  }
  return `summaries: structural — ${why ?? "no model wrote the latest one"}`;
}

export class VsCodeSummaries {
  /**
   * Set when a failure says asking again would fail the same way: consent
   * refused, blocked by quota or policy, or the named model not offered. Held
   * for this window only; a reload tries again, since each of those can
   * change without Cartograph hearing of it.
   */
  private ruledOut: string | undefined;
  /** Why the latest summary is structural, when this window knows. */
  private lastWhy: string | undefined;
  /** The permission question has been put to the person in this window. */
  private asked = false;
  /** They said yes to it, so the next request may raise VS Code's own dialog. */
  private permitted = false;

  constructor(
    private readonly carto: Carto,
    private readonly context: vscode.ExtensionContext,
    private readonly cwd: string,
    private readonly logDirArgs: () => string[],
    private readonly again: () => void,
  ) {}

  get isRuledOut(): boolean {
    return this.ruledOut !== undefined;
  }

  /** What the tooltip should add about why summaries are, or are not, synthesised. */
  get why(): string | undefined {
    return this.ruledOut ?? this.lastWhy;
  }

  /**
   * Summarise the sessions the engine handed off, one request each.
   *
   * *userInitiated* is true for the command palette's sync. VS Code asks for
   * consent on an extension's first request and says a request should only
   * follow a user action, so the timer and file-watcher runs never raise that
   * dialog on their own: they ask first, in a notification, once per window.
   */
  async run(awaiting: Awaiting[], userInitiated: boolean): Promise<void> {
    if (!awaiting.length || this.ruledOut) {
      return;
    }
    const setting = vscode.workspace
      .getConfiguration("cartograph")
      .get<string>("summaryModel", "auto")
      .trim() || "auto";
    const all = await vscode.lm.selectChatModels({ vendor: "copilot" });
    if (!all.length) {
      // Copilot Chat not signed in, or not activated yet this early. Nothing
      // is written: the sessions keep their one summary for a later run.
      this.lastWhy = "waiting for Copilot's models in VS Code";
      return;
    }
    const model = all.find((m) => m.id === setting) ?? all.find((m) => m.family === setting);
    if (!model) {
      await this.giveUp(awaiting, `model "${setting}" is not offered by Copilot here`,
        "cartograph.summaryModel names a model Copilot does not offer in this window");
      return;
    }
    const access = this.context.languageModelAccessInformation.canSendRequest(model);
    if (access === false) {
      await this.giveUp(awaiting, "permission to use Copilot's models was not given",
        "VS Code has not allowed Cartograph to use Copilot's models");
      return;
    }
    if (access === undefined && !userInitiated && !this.permitted) {
      this.lastWhy = "waiting for permission to use Copilot's models";
      this.askOnce();
      return;
    }
    const label = `vscode-lm:${model.id}`;
    for (const { session } of awaiting) {
      if (this.ruledOut) {
        await this.structural(session);
        continue;
      }
      const brief = await this.brief(session);
      if (brief === undefined) {
        continue;
      }
      const outcome = await ask(model, brief);
      if (outcome.ok) {
        await this.store(session, outcome.text, label);
        this.lastWhy = undefined;
        continue;
      }
      this.lastWhy = outcome.reason;
      await this.structural(session);
      if (outcome.stop) {
        this.ruleOut(outcome.reason);
      }
    }
  }

  private askOnce(): void {
    if (this.asked) {
      return;
    }
    this.asked = true;
    // Not awaited: the sync that got here should finish, not wait on a person.
    void vscode.window
      .showInformationMessage(
        "Cartograph: Copilot CLI is not available here. Cartograph can summarise " +
          "finished Copilot sessions into your project memory with Copilot's models " +
          "in VS Code, on your own Copilot plan. VS Code will ask you to confirm once.",
        "Allow",
        "Use structural summaries",
      )
      .then((choice) => {
        if (choice === "Allow") {
          this.permitted = true;
          this.again();
        } else if (choice === "Use structural summaries") {
          // Their own choice: no warning to tell them what they just chose.
          this.ruledOut = "structural summaries chosen for this window";
          this.again();
        }
      });
  }

  /** Every awaiting session structural, and VS Code's models ruled out for the window. */
  private async giveUp(awaiting: Awaiting[], reason: string, notice: string): Promise<void> {
    this.ruleOut(reason, notice);
    for (const { session } of awaiting) {
      await this.structural(session);
    }
  }

  /** Said once per window, not on every run: the tooltip carries it after. */
  private ruleOut(reason: string, notice?: string): void {
    if (this.ruledOut) {
      return;
    }
    this.ruledOut = reason;
    void vscode.window.showWarningMessage(
      `Cartograph: ${notice ?? reason}, so session summaries in this window are ` +
        "structural (a list of the prompts, no synthesis). Reload the window to try again.",
    );
  }

  private async brief(session: string): Promise<string | undefined> {
    const envelope = await this.carto.json<{ brief?: string }>(
      ["mem", "summarise", "--session", session, "--brief-only",
        ...this.logDirArgs(), "--repo", this.cwd],
      this.cwd,
    );
    // No brief is the engine saying the session needs none now — already
    // summarised, or too short. Its word is final; nothing is written here.
    return envelope.ok ? envelope.data.brief : undefined;
  }

  private async store(session: string, answer: string, label: string): Promise<void> {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cartograph-summary-"));
    const file = path.join(dir, "answer.txt");
    try {
      fs.writeFileSync(file, answer, "utf8");
      await this.carto.json(
        ["mem", "summarise", "--session", session, "--answer-file", file,
          "--summarised-by", label, ...this.logDirArgs(), "--repo", this.cwd],
        this.cwd,
      );
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  }

  private async structural(session: string): Promise<void> {
    await this.carto.json(
      ["mem", "summarise", "--session", session, "--no-host-agent", "--repo", this.cwd],
      this.cwd,
    );
  }
}

/**
 * One request, and what its failure means for the next one.
 *
 * `stop` is true where asking again in this window would fail the same way:
 * NoPermissions (consent refused), Blocked (quota or policy), NotFound (the
 * model went away). A timeout or an unspecified model error is this session's
 * problem only.
 */
async function ask(model: vscode.LanguageModelChat, brief: string): Promise<Outcome> {
  const source = new vscode.CancellationTokenSource();
  const timer = setTimeout(() => source.cancel(), TIMEOUT_MS);
  try {
    let tokens: number | undefined;
    try {
      tokens = await model.countTokens(brief, source.token);
    } catch {
      // Counting is a courtesy; the request itself says if the brief is too long.
    }
    if (tokens !== undefined && tokens > model.maxInputTokens) {
      return { ok: false, reason: `the brief is longer than ${model.id} accepts`, stop: false };
    }
    const response = await model.sendRequest(
      [vscode.LanguageModelChatMessage.User(brief)],
      { justification: JUSTIFICATION },
      source.token,
    );
    let text = "";
    for await (const chunk of response.text) {
      text += chunk;
    }
    if (source.token.isCancellationRequested) {
      return { ok: false, reason: "the model did not answer in time", stop: false };
    }
    return text.trim()
      ? { ok: true, text }
      : { ok: false, reason: "the model answered with nothing", stop: false };
  } catch (err) {
    if (source.token.isCancellationRequested) {
      return { ok: false, reason: "the model did not answer in time", stop: false };
    }
    if (err instanceof vscode.LanguageModelError) {
      if (err.code === vscode.LanguageModelError.NoPermissions.name) {
        return { ok: false, reason: "permission to use Copilot's models was not given", stop: true };
      }
      if (err.code === vscode.LanguageModelError.Blocked.name) {
        return { ok: false, reason: "Copilot blocked the request (quota or policy)", stop: true };
      }
      if (err.code === vscode.LanguageModelError.NotFound.name) {
        return { ok: false, reason: `model ${model.id} is no longer available`, stop: true };
      }
    }
    return { ok: false, reason: `the model request failed: ${String(err).slice(0, 120)}`, stop: false };
  } finally {
    clearTimeout(timer);
    source.dispose();
  }
}
