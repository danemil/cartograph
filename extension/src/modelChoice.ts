/**
 * Which of the models Copilot offers through `vscode.lm` writes a summary.
 *
 * Kept free of the `vscode` module so it runs under plain node: there is no
 * extension test host, and this is the rule a wrong guess about Copilot's
 * model list would break. The first version looked for one model — id or
 * family `auto` — and when a VS Code/plan combination did not offer it, every
 * session in the window got a structural summary. A missing preferred model
 * is not a reason to write no summary; any Copilot model writes a better one
 * than a list of prompts.
 *
 * The order:
 *   1. `cartograph.summaryModel`, when it is not `auto`: exact id, then exact
 *      family, then either ignoring case.
 *   2. Copilot's Auto model (id or family `auto`): it routes a short brief to
 *      a light model and never to one an administrator blocked.
 *   3. A light model, judged from its family, then id, then display name,
 *      split into words (so `gemini` is not read as `mini`). Family comes
 *      first because it is the field Copilot fills from the model's own name
 *      (`gpt-4o-mini`, `claude-3.5-haiku`); ids can be deployment names and
 *      names are for display. One whose known input limit is below a worst-case
 *      brief is passed over.
 *   4. The first model offered — Copilot's own order.
 */

/** What `vscode.LanguageModelChat` exposes that the choice may look at. */
export interface ModelInfo {
  id: string;
  family: string;
  name?: string;
  version?: string;
  vendor?: string;
  maxInputTokens?: number;
}

export interface Choice<M extends ModelInfo> {
  model: M;
  /** One line for the log: why this model. */
  why: string;
  /** The setting named a model (not `auto`) that is not offered here. */
  pinnedMissing: boolean;
}

/**
 * Words that mark a model built to be small and cheap. Matched as whole words
 * of the family/id/name, never as substrings.
 */
export const LIGHT_WORDS = ["mini", "nano", "luna", "flash", "haiku", "lite", "small"];

/**
 * The engine's worst-case brief is about 11k tokens (40 prompts × 1,100
 * characters, chars/4; see MAX_PROMPTS in summarise.py). A light model that
 * cannot take that is no bargain.
 */
export const MIN_INPUT_TOKENS = 12_000;

/** Undefined only when *offered* is empty. */
export function chooseModel<M extends ModelInfo>(
  offered: readonly M[],
  setting: string,
): Choice<M> | undefined {
  if (!offered.length) {
    return undefined;
  }
  const wanted = setting.trim() || "auto";
  const pinned = wanted.toLowerCase() !== "auto";
  let missing = "";
  if (pinned) {
    const found = named(offered, wanted);
    if (found) {
      return { model: found, why: `cartograph.summaryModel "${wanted}"`, pinnedMissing: false };
    }
    missing = `"${wanted}" not offered; `;
  }
  const auto = named(offered, "auto");
  if (auto) {
    return { model: auto, why: `${missing}Copilot's Auto model`, pinnedMissing: pinned };
  }
  missing = missing || `"auto" not offered; `;
  for (const model of offered) {
    const light = lightWord(model);
    if (light && !tooSmall(model)) {
      return {
        model,
        why: `${missing}light model ("${light.word}" in its ${light.field})`,
        pinnedMissing: pinned,
      };
    }
  }
  return { model: offered[0], why: `${missing}no light model; the first offered`, pinnedMissing: pinned };
}

function named<M extends ModelInfo>(offered: readonly M[], wanted: string): M | undefined {
  const lower = wanted.toLowerCase();
  return (
    offered.find((m) => m.id === wanted) ??
    offered.find((m) => m.family === wanted) ??
    offered.find((m) => m.id.toLowerCase() === lower || m.family.toLowerCase() === lower)
  );
}

function wordIn(text: string | undefined): string | undefined {
  const words = (text ?? "").toLowerCase().split(/[^a-z0-9]+/);
  return LIGHT_WORDS.find((word) => words.includes(word));
}

function lightWord(model: ModelInfo): { word: string; field: string } | undefined {
  for (const field of ["family", "id", "name"] as const) {
    const word = wordIn(model[field]);
    if (word) {
      return { word, field };
    }
  }
  return undefined;
}

function tooSmall(model: ModelInfo): boolean {
  return typeof model.maxInputTokens === "number" && model.maxInputTokens > 0
    && model.maxInputTokens < MIN_INPUT_TOKENS;
}

/** `id | family | vendor | maxInputTokens`, as the log shows each model. */
export function describeModel(model: ModelInfo): string {
  return `${model.id} | ${model.family} | ${model.vendor ?? "?"} | ${model.maxInputTokens ?? "?"}`;
}
