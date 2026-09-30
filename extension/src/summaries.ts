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
import * as log from "./log";
import { chooseModel, describeModel } from "./modelChoice";

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
  | { ok: false; reason: string; stop: boolean; code?: string };

/**
 * The engine's `--summarise` flags for this setting and this window's state.
 *
 * Once VS Code's models have been ruled out for the window, `auto` is the
 * CLI-then-structural path the engine had before, and `vscode` is structural:
 * someone who chose VS Code's models has said not to use the CLI. *ruledOut*
 * is why, and goes to the store with each structural summary, so `mem status`
 * can still say it after this window is gone.
 */
export function summariseArgs(host: SummaryHost, ruledOut: string | undefined): string[] {
  switch (host) {
    case "structural":
      return ["--summarise", "--no-host-agent"];
    case "cli":
      return ["--summarise"];
    case "vscode":
      return ruledOut
        ? ["--summarise", "--no-host-agent", "--fallback-reason", ruledOut]
        : ["--summarise", "--hand-off", "always"];
    default:
      // The engine records its own reason if the CLI is not there either.
      return ruledOut ? ["--summarise"] : ["--summarise", "--hand-off", "no-cli"];
  }
}

/**
 * The tooltip line for the newest summary, from `mem status`'s
 * `latest_summary_by`: a label, `structural`, or `structural (<reason>)`. The
 * stored reason is about that row; this window's is only a guess at it.
 */
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
  const stored = /^structural \((.*)\)$/s.exec(by)?.[1];
  return `summaries: structural — ${stored ?? why ?? "no model wrote the latest one"}`;
}

export class VsCodeSummaries {
  /**
   * Set when a failure says asking again would fail the same way: consent
   * refused, blocked by quota or policy, or the model withdrawn. Held
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
  /** The pinned-model notice has been shown in this window. */
  private pinnedSaid = false;

  constructor(
    private readonly carto: Carto,
    private readonly context: vscode.ExtensionContext,
    private readonly cwd: string,
    private readonly logDirArgs: () => string[],
    private readonly again: () => void,
  ) {}

  /** Why VS Code's models are not used in this window, or undefined. */
  get ruledOutReason(): string | undefined {
    return this.ruledOut;
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
    if (!awaiting.length) {
      return;
    }
    const config = vscode.workspace.getConfiguration("cartograph");
    const setting = config.get<string>("summaryModel", "auto").trim() || "auto";
    log.info(
      `summaries: ${awaiting.map((a) => a.session).join(", ")} awaiting · ` +
        `summaryHost=${config.get<string>("summaryHost", "auto")} summaryModel=${setting} · ` +
        (userInitiated ? "user-initiated" : "background"),
    );
    if (this.ruledOut) {
      log.info(`summaries: VS Code's models ruled out in this window (${this.ruledOut})`);
      return;
    }
    const all = await vscode.lm.selectChatModels({ vendor: "copilot" });
    log.info(`models offered: ${all.length ? all.map(describeModel).join(", ") : "none"}`);
    const choice = chooseModel(all, setting);
    if (!choice) {
      // Copilot Chat not signed in, or not activated yet this early. Nothing
      // is written: the sessions keep their one summary for a later run.
      this.lastWhy = "waiting for Copilot's models in VS Code";
      log.info("summaries: no Copilot models yet; nothing written, the sessions wait");
      return;
    }
    const { model } = choice;
    log.info(`model chosen: ${model.id} — ${choice.why}`);
    if (choice.pinnedMissing) {
      this.pinnedNotice(setting, model.id);
    }
    const access = this.context.languageModelAccessInformation.canSendRequest(model);
    log.info(`canSendRequest(${model.id}): ${access === undefined ? "not asked yet" : access}`);
    if (access === false) {
      await this.giveUp(awaiting, "permission to use Copilot's models was not given",
        "VS Code has not allowed Cartograph to use Copilot's models");
      return;
    }
    if (access === undefined && !userInitiated && !this.permitted) {
      this.lastWhy = "waiting for permission to use Copilot's models";
      log.info("summaries: waiting for permission; asking in a notification, nothing written");
      this.askOnce();
      return;
    }
    const label = `vscode-lm:${model.id}`;
    for (const { session } of awaiting) {
      if (this.ruledOut) {
        await this.structural(session, this.ruledOut);
        continue;
      }
      const brief = await this.brief(session);
      if (brief === undefined) {
        log.info(`session ${session}: the engine gave no brief (already summarised or too short)`);
        continue;
      }
      const outcome = await ask(model, brief);
      if (outcome.ok) {
        if (await this.store(session, outcome.text, label)) {
          this.lastWhy = undefined;
          log.info(`session ${session}: host-agent via ${label}`);
        }
        continue;
      }
      this.lastWhy = outcome.reason;
      log.warn(
        `session ${session}: structural — ${outcome.reason}` +
          (outcome.code ? ` [LanguageModelError ${outcome.code}]` : ""),
      );
      await this.structural(session, outcome.reason);
      if (outcome.stop) {
        this.ruleOut(outcome.reason);
      } else if (userInitiated) {
        this.sessionNotice(outcome.reason);
      }
    }
  }

  /**
   * A pinned model that is not offered is used instead of, not as well as —
   * say which, once per window, so the setting can be corrected.
   */
  private pinnedNotice(setting: string, used: string): void {
    if (this.pinnedSaid) {
      return;
    }
    this.pinnedSaid = true;
    log.warn(`cartograph.summaryModel "${setting}" is not offered here; using ${used}`);
    void vscode.window
      .showWarningMessage(
        `Cartograph: cartograph.summaryModel "${setting}" is not offered by Copilot here; ` +
          `summarising with ${used} instead.`,
        "Show Log",
        "Settings",
      )
      .then((choice) => {
        if (choice === "Show Log") {
          log.show();
        } else if (choice === "Settings") {
          void vscode.commands.executeCommand(
            "workbench.action.openSettings", "cartograph.summaryModel",
          );
        }
      });
  }

  /**
   * One session came out structural for a reason that is not the window's
   * (an error, a timeout). Only said when a person ran the sync and is
   * waiting on it; a background run leaves it to the log and the tooltip.
   */
  private sessionNotice(reason: string): void {
    void vscode.window
      .showWarningMessage(
        `Cartograph: a session summary is structural (a list of the prompts, no synthesis): ${reason}.`,
        "Show Log",
      )
      .then((choice) => {
        if (choice === "Show Log") {
          log.show();
        }
      });
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
        log.info(`permission notification: ${choice ?? "dismissed"}`);
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
      log.warn(`session ${session}: structural — ${reason}`);
      await this.structural(session, reason);
    }
  }

  /** Said once per window, not on every run: the tooltip carries it after. */
  private ruleOut(reason: string, notice?: string): void {
    if (this.ruledOut) {
      return;
    }
    this.ruledOut = reason;
    log.warn(`VS Code's models ruled out for this window: ${reason}`);
    void vscode.window
      .showWarningMessage(
        `Cartograph: ${notice ?? reason}, so session summaries in this window are ` +
          "structural (a list of the prompts, no synthesis). Reload the window to try again.",
        "Show Log",
      )
      .then((choice) => {
        if (choice === "Show Log") {
          log.show();
        }
      });
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

  private async store(session: string, answer: string, label: string): Promise<boolean> {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cartograph-summary-"));
    const file = path.join(dir, "answer.txt");
    try {
      fs.writeFileSync(file, answer, "utf8");
      const envelope = await this.carto.json(
        ["mem", "summarise", "--session", session, "--answer-file", file,
          "--summarised-by", label, ...this.logDirArgs(), "--repo", this.cwd],
        this.cwd,
      );
      if (!envelope.ok) {
        log.warn(`session ${session}: the engine did not store the answer: ${envelope.error?.message}`);
      }
      return envelope.ok;
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  }

  /** *reason* goes into the store with the row, for `mem status` to report. */
  private async structural(session: string, reason: string): Promise<void> {
    const envelope = await this.carto.json(
      ["mem", "summarise", "--session", session, "--no-host-agent",
        "--fallback-reason", reason, "--repo", this.cwd],
      this.cwd,
    );
    if (!envelope.ok) {
      log.warn(`session ${session}: the engine did not store a structural summary: ${envelope.error?.message}`);
    }
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
    const code = err instanceof vscode.LanguageModelError ? err.code : undefined;
    if (code === vscode.LanguageModelError.NoPermissions.name) {
      return { ok: false, reason: "permission to use Copilot's models was not given", stop: true, code };
    }
    if (code === vscode.LanguageModelError.Blocked.name) {
      return { ok: false, reason: "Copilot blocked the request (quota or policy)", stop: true, code };
    }
    if (code === vscode.LanguageModelError.NotFound.name) {
      return { ok: false, reason: `model ${model.id} is no longer available`, stop: true, code };
    }
    return {
      ok: false, reason: `the model request failed: ${String(err).slice(0, 120)}`, stop: false, code,
    };
  } finally {
    clearTimeout(timer);
    source.dispose();
  }
}
