/**
 * Sessions the engine is waiting on before it will summarise them.
 *
 * `mem sync --summarise` summarises a session only once its last message is
 * 30 minutes old, and lists the ones not there yet as `waiting`. Without this,
 * a Sync Memory that hands nothing over looks exactly like one that found
 * nothing — which is how a finished session on the Remote SSH VM went
 * unexplained. No `vscode` import, so `test/waiting.js` can run it under node.
 */

export interface Waiting {
  session: string;
  /** ISO 8601, UTC. */
  last_message_at: string;
  /** ISO 8601, UTC: when a sync will first take it. */
  settles_at: string;
  /** Set when no log had message timestamps and the file's mtime was used. */
  reason?: string;
}

/** Local HH:MM — the reader compares it with the clock on their screen. */
export function clock(iso: string): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) {
    return iso;
  }
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(at.getHours())}:${pad(at.getMinutes())}`;
}

/** Enough of a session id to recognise; the engine shortens titles the same way. */
function short(session: string): string {
  return session.length > 8 ? `${session.slice(0, 8)}…` : session;
}

/** The log line's part: "1 waiting: f2f66c3b… last message 22:07, settles 22:37". */
export function describeWaiting(waiting: Waiting[] | undefined): string {
  if (!waiting?.length) {
    return "";
  }
  const each = waiting.map((w) =>
    `${short(w.session)} last message ${clock(w.last_message_at)}, settles ${clock(w.settles_at)}` +
      (w.reason ? ` (${w.reason})` : ""),
  );
  return `${waiting.length} waiting: ${each.join("; ")}`;
}

/**
 * The Sync Memory notification's sentence, when a person ran it and nothing
 * was summarised or handed over only because sessions have not settled.
 * Undefined otherwise: a run that did summarise has its own result to report.
 */
export function waitingNotice(data: {
  waiting?: Waiting[];
  awaiting_summary?: unknown[];
  summarised?: unknown[];
} | undefined): string | undefined {
  const waiting = data?.waiting ?? [];
  if (!waiting.length || data?.awaiting_summary?.length || data?.summarised?.length) {
    return undefined;
  }
  // The engine's settle time, from its own answer rather than a copy here.
  const [w] = waiting;
  const minutes = Math.round((Date.parse(w.settles_at) - Date.parse(w.last_message_at)) / 60_000);
  if (waiting.length === 1) {
    return `No session summarised yet: its last message was at ${clock(w.last_message_at)}, ` +
      `and a session is summarised ${minutes} minutes after its last message ` +
      `(from ${clock(w.settles_at)}).`;
  }
  const first = waiting.map((x) => x.settles_at).sort()[0];
  return `No session summarised yet: ${waiting.length} sessions had a message in the last ` +
    `${minutes} minutes; the first can be summarised from ${clock(first)}.`;
}
