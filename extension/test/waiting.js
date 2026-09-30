// Plain-node check of what the log line and the Sync Memory notification say
// about sessions still settling (src/waiting.ts). Run after `npm run -s compile`:
// node test/waiting.js
process.env.TZ = "UTC"; // clock() prints local time; pin it so the expectations hold
const assert = require("assert");
const { describeWaiting, waitingNotice } = require("../out/waiting.js");

const f2f6 = {
  session: "f2f66c3b-1c2d-4e5f-8a9b-0c1d2e3f4a5b",
  last_message_at: "2026-09-30T22:07:12Z",
  settles_at: "2026-09-30T22:37:12Z",
};
const bare = { session: "s1", last_message_at: "2026-09-30T22:10:00Z",
  settles_at: "2026-09-30T22:40:00Z",
  reason: "no message timestamps in transcripts/s1.jsonl; using its modification time" };

const checks = [
  ["log line, one", describeWaiting([f2f6]),
    "1 waiting: f2f66c3b… last message 22:07, settles 22:37"],
  ["log line, mtime fallback", describeWaiting([bare]),
    "1 waiting: s1 last message 22:10, settles 22:40 (no message timestamps in transcripts/s1.jsonl; using its modification time)"],
  ["log line, none", describeWaiting([]), ""],
  ["notice, nothing handed over", waitingNotice({ waiting: [f2f6], awaiting_summary: [] }),
    "No session summarised yet: its last message was at 22:07, and a session is summarised 30 minutes after its last message (from 22:37)."],
  ["notice, two", waitingNotice({ waiting: [bare, f2f6], summarised: [] }),
    "No session summarised yet: 2 sessions had a message in the last 30 minutes; the first can be summarised from 22:37."],
  ["notice, something handed over", waitingNotice({ waiting: [f2f6], awaiting_summary: [{ session: "x", prompts: 2 }] }), undefined],
  ["notice, something summarised", waitingNotice({ waiting: [f2f6], summarised: [{}] }), undefined],
  ["notice, nothing waiting", waitingNotice({ waiting: [], awaiting_summary: [] }), undefined],
  ["notice, no result", waitingNotice(undefined), undefined],
];
for (const [name, got, want] of checks) {
  assert.strictEqual(got, want, name);
  console.log(`ok  ${name}`);
}
console.log(`${checks.length} passed`);
