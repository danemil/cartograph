// Plain-node check of the version-skew rule (src/payload.ts, sameRelease).
// `carto --version` prints "cartograph <release> (engine fork of
// code-review-graph <upstream>)"; only the release may match the payload's.
// Run after `npm run -s compile`: node test/version.js
const assert = require("assert");
const { sameRelease } = require("../out/payload.js");

const line = "cartograph 0.8.4 (engine fork of code-review-graph 2.3.8)";
const checks = [
  ["same release", sameRelease(line, "0.8.4"), true],
  ["other release", sameRelease(line, "0.8.5"), false],
  ["upstream is not the release", sameRelease(line, "2.3.8"), false],
  ["prefix of a longer release", sameRelease("cartograph 0.8.41 (engine fork of code-review-graph 2.3.8)", "0.8.4"), false],
  ["pre-0.9 binary (upstream only)", sameRelease("cartograph 2.3.8", "0.8.4"), false],
  ["trailing newline", sameRelease(line + "\n", "0.8.4"), true],
  ["not a version line", sameRelease("Traceback (most recent call last):", "0.8.4"), false],
];
let failed = 0;
for (const [name, got, want] of checks) {
  try {
    assert.strictEqual(got, want);
  } catch {
    failed++;
    console.log(`FAIL ${name}: got ${got}, want ${want}`);
  }
}
console.log(`${checks.length - failed}/${checks.length} version checks`);
process.exit(failed ? 1 : 0);
