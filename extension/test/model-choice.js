// Plain-node check of the summary model rule (src/modelChoice.ts). There is no
// extension test host; this rule is the part a wrong guess about Copilot's
// model list breaks. Run after `npm run -s compile`: node test/model-choice.js
const assert = require("assert");
const { chooseModel } = require("../out/modelChoice.js");

const m = (id, family, extra = {}) => ({ id, family, vendor: "copilot", maxInputTokens: 128000, ...extra });
const cases = [
  ["auto offered", [m("gpt-5", "gpt-5"), m("auto", "auto")], "auto", "auto", false],
  ["auto by family only", [m("gpt-5", "gpt-5"), m("copilot-auto", "auto")], "auto", "copilot-auto", false],
  ["no auto: light by family", [m("gpt-5", "gpt-5"), m("claude-sonnet-4.5", "claude-sonnet-4.5"), m("gpt-5-mini", "gpt-5-mini")], "auto", "gpt-5-mini", false],
  ["gemini is not mini", [m("gemini-2.5-pro", "gemini-2.5-pro"), m("claude-haiku-4.5", "claude-haiku-4.5")], "auto", "claude-haiku-4.5", false],
  ["no light: first offered", [m("gemini-2.5-pro", "gemini-2.5-pro"), m("gpt-5", "gpt-5")], "auto", "gemini-2.5-pro", false],
  ["light but too small", [m("gpt-5", "gpt-5"), m("tiny-nano", "tiny-nano", { maxInputTokens: 4096 }), m("gpt-6-luna", "gpt-6-luna")], "auto", "gpt-6-luna", false],
  ["light by name only", [m("gpt-5", "gpt-5"), m("x1", "x1", { name: "Gemini 2.0 Flash" })], "auto", "x1", false],
  ["pinned family", [m("auto", "auto"), m("gpt-4o-2024", "gpt-4o")], "gpt-4o", "gpt-4o-2024", false],
  ["pinned, other case", [m("auto", "auto"), m("GPT-4o", "GPT-4o")], "gpt-4o", "GPT-4o", false],
  ["pinned missing: auto", [m("gpt-5", "gpt-5"), m("auto", "auto")], "claude-x", "auto", true],
  ["pinned missing: light", [m("gpt-5", "gpt-5"), m("o4-mini", "o4-mini")], "claude-x", "o4-mini", true],
  ["blank setting is auto", [m("gpt-5", "gpt-5")], "  ", "gpt-5", false],
];
for (const [name, list, setting, id, pinnedMissing] of cases) {
  const choice = chooseModel(list, setting);
  assert.strictEqual(choice.model.id, id, name);
  assert.strictEqual(choice.pinnedMissing, pinnedMissing, name);
  console.log(`ok  ${name.padEnd(26)} ${choice.model.id.padEnd(18)} ${choice.why}`);
}
assert.strictEqual(chooseModel([], "auto"), undefined);
console.log(`ok  empty list: no choice\n${cases.length + 1} passed`);
