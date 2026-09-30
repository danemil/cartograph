"""Synthesis across one session, not a rewording of each prompt in it.

:mod:`cartograph.mem.ingest` records prompts verbatim, one row each. Those rows
are the evidence and nothing here rewrites or removes them. A single captured
prompt is already short and already in the words a person chose; paraphrasing
one individually would buy nothing and cost an inference call.

What no row holds is the shape of the *session*: what was worked on, what was
decided, and which attempts turned out to be dead ends. The last of those is
why this module exists — nothing else in a repository records what was
abandoned. So the unit of summarisation is a session, and the output is exactly
one new observation with ``doc_type="sessions"``, distinct from the ``prompts``
rows it was derived from.

T07 names the host agent as the source of that prose and a deterministic
structural summary as the fallback, and requires the two to stay
distinguishable: ``summary_source`` is ``host-agent`` only when a host actually
answered. A structural summary is never dressed up as a host-agent one, and a
guard trip is a normal input to the fallback rather than an error.

Three things bound what this costs, each with its reason at the constant:
:data:`MIN_PROMPTS`, :data:`MAX_PROMPTS` × :data:`MAX_PROMPT_CHARS`,
:data:`HOST_TIMEOUT_SECONDS`, and the one-summary-per-session rule enforced in
:func:`summarise`. Cartograph itself makes no network call: the host agent is
already installed and already authenticated, and on a machine where neither CLI
is present — the common locked-down case — the structural path carries it
without complaint.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from . import ingest as _ingest
from . import store as _store

logger = logging.getLogger(__name__)

#: Set in the environment of a host agent this module shells out to, and
#: checked before shelling out. It is the depth cap: at depth 1 a summarisation
#: is in progress somewhere above, and a second host call from inside the first
#: one is the loop T07 calls the load-bearing constraint.
#:
#: Distinct from ``CARTO_HOOK_ACTIVE``, which gates *hooks*. The two cannot be
#: one variable: the hook that starts a summarisation sets the hook marker in
#: the child it spawns, so a summariser reading that marker would refuse to
#: call a host on exactly the path that is supposed to.
SUMMARISE_MARKER = "CARTO_SUMMARISE_ACTIVE"

#: Below this there is nothing to synthesise across. One prompt's verbatim row
#: already says what it says, and a "summary" of it would be the reworded copy
#: this design rejects. Not an error — the honest answer is that the session
#: did not accumulate enough to be worth a summary.
MIN_PROMPTS = 2

#: Prompts fed to the host, most recent first in selection and chronological in
#: presentation (see :meth:`MemoryStore.session_documents`). The capture cap is
#: 100 per session, so without this the brief is unbounded from the host's
#: point of view while looking bounded from the store's.
MAX_PROMPTS = 40

#: Characters of each prompt that reach the brief. Capture keeps up to
#: ``ingest.MAX_BODY_CHARS`` (4,000) verbatim, which is right for evidence and
#: wrong for a brief: 40 × 4,000 is a 160k-character input for a few hundred
#: words of output. The request is in a prompt's opening lines; the rest is
#: pasted context the verbatim row still holds.
#:
#: Worst case, therefore: 40 × 500 ≈ 20k characters ≈ 5k tokens per session.
MAX_PROMPT_CHARS = 500

#: Characters of each turn's final assistant reply that reach the brief. The
#: reply is where a turn's conclusion is; its opening is usually the answer and
#: the rest the working. Worst case with :data:`MAX_PROMPTS`: 40 × (500 + 600)
#: ≈ 44k characters ≈ 11k tokens per session summary.
MAX_REPLY_CHARS = 600

#: ``mem_meta`` keys for what memory cost. Host calls are counted when made,
#: failed ones included — a failed call still spent quota. The raw size of a
#: summarised session is what reading it back unsummarised would have cost.
COST_HOST_CALLS = "cost:host_calls"
COST_RAW_CHARS = "cost:raw_chars:"

#: A host call measured 3–8 seconds on the machine this was built on, for a
#: prompt of this size. Ten times that is headroom for a slow model or a cold
#: start; past it the structural summary is worth more than the wait, and this
#: process may be a detached child nobody is watching.
HOST_TIMEOUT_SECONDS = 90

#: Sessions one ``--pending`` run will summarise. Each is an inference call on
#: the person's Copilot quota, started by nothing more deliberate than opening
#: a new chat; a backlog left by a machine that ran without a host CLI for a
#: month should drain over several sessions, not all at once.
MAX_PENDING = 3

#: What ``platform_source`` says when the answer came from outside the engine:
#: the VS Code extension, through ``vscode.lm``, naming the model it used. Held
#: to one shape because the field is how a later reader tells which model wrote
#: a summary, and a caller must not be able to claim ``copilot-cli`` for text
#: the CLI never saw.
ANSWER_LABEL = re.compile(r"^vscode-lm:[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")

#: ``--hand-off`` values. ``no-cli``: hand off only what no CLI host will
#: summarise here. ``always``: skip the CLI even where it is installed, for an
#: organisation that wants summaries through VS Code's model policy alone.
HAND_OFF_MODES = ("no-cli", "always")

#: `sessions` for the row written, `prompts` for the rows read. Named once so
#: the two never drift into meaning the same thing.
SESSION_DOC_TYPE = "sessions"
PROMPT_DOC_TYPE = "prompts"


@dataclass(frozen=True)
class Host:
    """One host agent CLI, and the exact flags it is invoked with.

    Every flag below was read out of the host's own ``--help`` and then run,
    rather than recalled. That matters more than it looks: a flag that does not
    exist turns the primary path into a silent, permanent fallback, and the
    only visible symptom is ``summary_source`` reading ``structural`` forever.
    """

    #: What goes in ``platform_source`` when this host writes the summary.
    name: str
    binary: str
    #: Argv around the brief: ``[binary, *before, brief, *after]``.
    before: tuple[str, ...]
    after: tuple[str, ...]
    #: What in this invocation stops the host running its own hooks, or "" when
    #: the host exposes nothing for it. Recorded because T07 requires the guard
    #: to be three things and this is the third; where it is empty, the env
    #: marker and the depth cap are carrying the whole load.
    no_hooks_flag: str
    #: The host's flag for choosing a model, or "" when it has none worth
    #: setting. Given :func:`summary_model`'s value at call time.
    model_flag: str = ""

    def argv(self, brief: str) -> list[str]:
        model = [self.model_flag, summary_model()] if self.model_flag else []
        return [self.binary, *self.before, brief, *self.after, *model]


def summary_model() -> str:
    """The model a summary is written with: ``auto`` unless pinned.

    ``auto`` because Copilot chooses by task complexity — a light model for a
    short brief (measured: ``gpt-6-luna``) — costs 10% less on paid plans, and
    never chooses a model an administrator blocked. A pinned name that policy
    blocks would fail every call and turn every summary structural without a
    word. ``CARTO_SUMMARY_MODEL`` pins one for anyone who has measured better.
    """
    return os.environ.get("CARTO_SUMMARY_MODEL", "").strip() or "auto"


#: Copilot only: it is the one AI tool the target environment allows, and
#: session text must never be sent anywhere else.
HOSTS: tuple[Host, ...] = (
    Host(
        name="copilot-cli",
        binary="copilot",
        before=("-p",),
        after=(
            # --silent: the response and nothing else, so stdout is the summary
            # rather than the summary wrapped in session statistics.
            "--silent",
            # No AGENTS.md: this call is summarising text, and a repository's
            # instructions for writing code would only steer it off.
            "--no-custom-instructions",
            # MCP is banned in the target organisation, and the built-in
            # GitHub server is egress this call has no use for.
            "--disable-builtin-mcps",
            # A summarisation must never be the thing that downloads a CLI
            # update on a default-deny machine.
            "--no-auto-update",
            # No log file per background summarisation.
            "--log-level",
            "none",
        ),
        # Copilot CLI has no per-run equivalent: hooks are disabled only by the
        # persisted `disableAllHooks` config key, and Cartograph will not
        # rewrite a person's config to make its own call cheaper.
        no_hooks_flag="",
        model_flag="--model",
    ),
)

#: The shape asked of the host. Prescriptive because the answer is stored and
#: searched, not read once: a free-form summary would vary in what it covers,
#: and `mem search` would then find sessions unevenly for reasons nothing
#: records. DEAD ENDS is called out because it is the line with no substitute
#: anywhere else in the repository.
_BRIEF = """\
Summarise one coding session for a project memory that later sessions search.

Below is the session, turn by turn: what the person typed, and where the log
has it, the assistant's final reply to that turn. You cannot see the code, so
write only what the text supports. Where it does not say whether something
worked, say that rather than guessing.

Answer in exactly this shape. Plain text, no markdown, no preamble:

TITLE: <one line under 100 characters naming what this session was about>
WORKED ON: <one or two sentences>
DECIDED: <only what the PERSON stated or accepted, and why if the text says;
"none" if nothing was>
PROPOSED: <what the assistant suggested that the person did not confirm;
"none" if nothing was>
DEAD ENDS: <what was tried and abandoned, and why; "none" if nothing was>

DECIDED must never contain a suggestion the person did not take up — a later
session will treat it as settled. DEAD ENDS is the most valuable line: nothing
else in a repository records what was abandoned.

SESSION:
{prompts}
"""

_TITLE_LINE = re.compile(r"^\s*TITLE\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


# ---------------------------------------------------------------------------
# The brief, and the summary when no host writes one
# ---------------------------------------------------------------------------


def _prompt_text(row: dict[str, Any]) -> str:
    """One prompt as the brief carries it, bounded by :data:`MAX_PROMPT_CHARS`."""
    body = (row.get("body") or row.get("title") or "").strip()
    if len(body) <= MAX_PROMPT_CHARS:
        return body
    return body[:MAX_PROMPT_CHARS].rstrip() + " […]"


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit].rstrip() + " […]"


def brief(
    prompts: "list[dict[str, Any]]", replies: "Optional[list[Optional[str]]]" = None
) -> str:
    """The text sent to a host agent. Pure, so the budget is testable.

    *replies* aligns with *prompts*; a turn with no reply in any log shows the
    prompt alone.
    """
    turns = []
    for index, row in enumerate(prompts, start=1):
        turn = f"{index}. PERSON: {_prompt_text(row)}"
        reply = replies[index - 1] if replies and index - 1 < len(replies) else None
        if reply:
            turn += f"\n   ASSISTANT (final reply): {_clip(reply, MAX_REPLY_CHARS)}"
        turns.append(turn)
    return _BRIEF.format(prompts="\n\n".join(turns))


def structural_summary(
    session: str, prompts: "list[dict[str, Any]]"
) -> "tuple[str, str]":
    """``(title, body)`` derived from the rows alone, with no model involved.

    It shares no failure mode with the host path, which is the whole reason it
    is the fallback: no process to spawn, no network, no recursion surface, and
    the same output every time for the same rows.

    What it can honestly claim is an index of the session — every prompt's
    first line, in the order they were sent — and it says so in the body rather
    than implying a synthesis it did not perform. A lesser observation beats a
    missing one; a lesser observation *presented as* a better one does not.
    """
    first, last = prompts[0], prompts[-1]
    lines = [
        "Structural summary: no host agent wrote this. It lists what was asked, "
        "in order; it does not say what was decided or abandoned.",
        "",
        f"{len(prompts)} prompt(s) recorded between {first['created_at']} "
        f"and {last['created_at']}.",
        "",
    ]
    lines += [
        f"{index}. {_ingest.title_for(row.get('title') or row.get('body') or '')}"
        for index, row in enumerate(prompts, start=1)
    ]
    title = _ingest.title_for(
        f"Session {_short(session)}: {len(prompts)} prompts, "
        f"{_ingest.title_for(first.get('title') or '')}"
    )
    return title, _ingest.clip_body("\n".join(lines), by="summarise")


def _short(session: str) -> str:
    """Enough of a session id to recognise, short enough for a title."""
    return session[:8] if len(session) > 8 else session


def split_title(text: str) -> "tuple[Optional[str], str]":
    """A host answer split into its ``TITLE:`` line and the rest.

    A missing TITLE line is not a failed call: the body is still the host's
    synthesis and still what ``summary_source`` describes. The caller falls
    back to the structural title and keeps the host body, rather than throwing
    away an inference it already paid for.
    """
    match = _TITLE_LINE.search(text)
    if match is None:
        return None, text.strip()
    body = (text[: match.start()] + text[match.end() :]).strip()
    return _ingest.title_for(match.group(1)), body


# ---------------------------------------------------------------------------
# Calling a host
# ---------------------------------------------------------------------------


def available_hosts() -> "list[Host]":
    """The hosts installed on this machine.

    Empty is the expected answer on the machines this targets, not a problem to
    report: the acceptance test is a box with only VS Code on it, and plenty of
    them will have no agent CLI on PATH at all.
    """
    return [host for host in HOSTS if shutil.which(host.binary)]


def _host_env() -> dict[str, str]:
    """The environment a host agent is given.

    Both markers, for two different jobs. ``CARTO_HOOK_ACTIVE`` makes every
    ``carto hook`` inside the host's session a no-op, so the session this call
    creates is not itself captured and summarised — the marker is inherited by
    the whole process tree, including the hook subprocesses the host spawns
    itself. :data:`SUMMARISE_MARKER` caps the depth at one, for anything the
    hook marker does not cover.
    """
    from ..hook import REENTRY_MARKER

    env = dict(os.environ)
    env[REENTRY_MARKER] = "1"
    env[SUMMARISE_MARKER] = "1"
    return env


def call_host(host: Host, text: str) -> Optional[str]:
    """The host's answer, or None with the reason logged.

    Blocking, with a timeout. Detaching belongs one level up — the hook launches
    this whole command through ``spawn_detached`` and never waits for it — so
    doing it again here would leave nobody to notice the call failed and write
    the fallback.

    ``cwd`` is the system temp directory, deliberately never the repository —
    and it has to be set, because the detached child this runs in was started
    *in* the repository. The host is summarising text already in the argv;
    the checkout would only widen what a prompt could talk it into, and a
    trusted checkout's ``.github/hooks`` would load into the session this call
    creates.
    """
    try:
        proc = subprocess.run(  # noqa: S603 — argv is built here, never user text
            host.argv(text),
            capture_output=True,
            text=True,
            timeout=HOST_TIMEOUT_SECONDS,
            env=_host_env(),
            stdin=subprocess.DEVNULL,
            cwd=tempfile.gettempdir(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("%s did not answer: %s", host.binary, exc)
        return None
    if proc.returncode != 0:
        logger.debug("%s exited %d: %s", host.binary, proc.returncode, proc.stderr[:200])
        return None
    answer = (proc.stdout or "").strip()
    return answer or None


# ---------------------------------------------------------------------------
# The operation
# ---------------------------------------------------------------------------


def summarise(
    repo_root: Path,
    *,
    session: Optional[str] = None,
    project: Optional[str] = None,
    use_host: bool = True,
    user_dirs: "Optional[Iterable[Path]]" = None,
    workspace_dirs: "Iterable[Path]" = (),
    brief_only: bool = False,
    answer: Optional[str] = None,
    summarised_by: Optional[str] = None,
) -> dict[str, Any]:
    """Summarise one session into one new observation. Never raises, but for a bad label.

    Returns what `carto mem summarise` puts in ``data``. The ``observation``
    key is None for every outcome that wrote nothing, and the ``summary`` line
    says which outcome it was — a caller that cannot tell "already summarised"
    from "summarised now" would re-run it forever.

    *brief_only* returns the brief a host would be sent and writes nothing.
    *answer* is a host answer obtained by the caller — the extension, through
    ``vscode.lm`` — stored exactly as a CLI answer would be, with
    *summarised_by* as its ``platform_source``. Both pass every check below
    first, so a caller cannot brief or store a session that the one-summary
    rule or :data:`MIN_PROMPTS` would have refused.
    """
    if answer is not None and not ANSWER_LABEL.match(summarised_by or ""):
        raise ValueError(
            "an answer needs --summarised-by vscode-lm:<model-id>, "
            f"not {summarised_by!r}"
        )
    path = _store.db_path(repo_root, create=False)
    # Read, then close, then compose, then reopen to write. The host call can
    # take a minute and a half; holding the connection across it would put a
    # concurrent `prompt-capture` into SQLite's busy wait for that whole time,
    # inside a hook that has thirty seconds before its host kills it.
    with _store.MemoryStore(path) as memory:
        session = session or memory.latest_session(doc_type=PROMPT_DOC_TYPE)
        if not session:
            return {
                "summary": "No captured prompts to summarise.",
                "session": None,
                "observation": None,
            }

        # Budget rule: one summary per session, ever. A second call would spend
        # a second inference to produce a near-identical row, and the store
        # would then hold two records each claiming to be *the* summary of that
        # session. Counted through `session_count`, so it counts the same rows
        # `mem search --session --doc-type sessions` returns.
        if memory.session_count(session, doc_type=SESSION_DOC_TYPE):
            return {
                "summary": f"Session {session} already has a summary; not re-summarising.",
                "session": session,
                "already_summarised": True,
                "observation": None,
            }

        prompts = memory.session_documents(
            session, doc_type=PROMPT_DOC_TYPE, limit=MAX_PROMPTS
        )

    if len(prompts) < MIN_PROMPTS:
        return {
            "summary": (
                f"Session {session} recorded {len(prompts)} prompt(s); "
                f"{MIN_PROMPTS} are needed before a summary says more than they do."
            ),
            "session": session,
            "prompts_available": len(prompts),
            "observation": None,
        }

    replies, raw_chars = _replies_for(
        repo_root, session, prompts, user_dirs=user_dirs, workspace_dirs=workspace_dirs
    )
    if brief_only:
        return {
            "summary": (
                f"Brief for session {session} from {len(prompts)} prompt(s); nothing written."
            ),
            "session": session,
            "prompts": len(prompts),
            "brief": brief(prompts, replies),
            "observation": None,
        }
    if answer is not None:
        title, body, source, label, reason, attempts = _from_answer(
            session, prompts, answer, summarised_by or ""
        )
    else:
        title, body, source, host, reason, attempts = _compose(
            session, prompts, replies, use_host=use_host
        )
        label = host.name if host else None
    with _store.MemoryStore(path) as memory:
        memory.set_meta(
            COST_HOST_CALLS, str(int(memory.get_meta(COST_HOST_CALLS) or 0) + attempts)
        )
        memory.set_meta(COST_RAW_CHARS + session, str(raw_chars))
        observation = memory.add(
            project=project or repo_root.name,
            title=title,
            body=body,
            kind="session",
            session=session,
            # The row is a different KIND OF RECORD from the prompts it came
            # from, not a different observation type — which is what doc_type
            # is for, and why an agent searching for one rarely wants the other.
            doc_type=SESSION_DOC_TYPE,
            # Which host wrote the text, or nothing when no host did. The
            # session's own host stays on its prompt rows; duplicating it here
            # would make this field mean two things.
            platform_source=label,
            summary_source=source,
        )

    via = None
    if source == "host-agent":
        via = summarised_by if answer is not None else host.binary
    result: dict[str, Any] = {
        "summary": (
            f"Summarised session {session} from {len(prompts)} prompt(s) "
            f"({source}{f' via {via}' if via else ''})"
        ),
        "session": session,
        "prompts_summarised": len(prompts),
        "summary_source": source,
        "host": via,
        "observation": observation,
    }
    if reason:
        # Why the structural path ran, on the response as well as in the row's
        # `summary_source`. A guard trip is expected input to the fallback, so
        # it is reported as a fact about this call rather than as an error.
        result["fallback_reason"] = reason
    return result


def summarise_pending(
    repo_root: Path,
    *,
    exclude: "str | Iterable[str] | None" = None,
    project: Optional[str] = None,
    use_host: bool = True,
    user_dirs: "Optional[Iterable[Path]]" = None,
    workspace_dirs: "Iterable[Path]" = (),
) -> list[dict[str, Any]]:
    """Summarise the sessions that ended without anything saying so.

    One :func:`summarise` result per session attempted, most recent first, at
    most :data:`MAX_PENDING`. *exclude* is the session starting now, or the
    sessions whose logs are still changing: they have not finished, and
    summarising one would spend the one summary it is allowed on part of it.
    """
    excluded = [exclude] if isinstance(exclude, str) else list(exclude or ())
    path = _store.db_path(repo_root, create=False)
    with _store.MemoryStore(path) as memory:
        sessions = memory.unsummarised_sessions(
            source_type=PROMPT_DOC_TYPE,
            summary_type=SESSION_DOC_TYPE,
            min_rows=MIN_PROMPTS,
            exclude=excluded,
            limit=MAX_PENDING,
        )
    return [
        summarise(
            repo_root, session=session, project=project, use_host=use_host,
            user_dirs=user_dirs, workspace_dirs=workspace_dirs,
        )
        for session in sessions
    ]


def _replies_for(
    repo_root: Path,
    session: str,
    prompts: "list[dict[str, Any]]",
    *,
    user_dirs: "Optional[Iterable[Path]]",
    workspace_dirs: "Iterable[Path]",
) -> "tuple[list[Optional[str]], int]":
    """Each prompt's final reply from the logs, and the session's raw size.

    Matched by the prompt as stored, because the stored body is the captured
    text clipped the same way capture clips it. The raw size is every prompt
    and every reply at full length — what reading the session back without a
    summary would cost — and is measured whether or not any reply was found.
    """
    from . import sync as _sync

    try:
        turns = _sync.session_exchanges(
            repo_root, session, user_dirs=user_dirs, workspace_dirs=workspace_dirs
        )
    except Exception as exc:  # noqa: BLE001 — a missing log must not cost the summary
        logger.debug("no exchanges for %s: %s", session, exc)
        turns = []
    by_body = {_ingest.clip_body(prompt, by="capture"): reply for prompt, reply in turns}
    replies = [by_body.get(row.get("body") or "") for row in prompts]
    if turns:
        raw = sum(len(prompt) + len(reply or "") for prompt, reply in turns)
    else:
        raw = sum(len(row.get("body") or "") for row in prompts)
    return replies, raw


def _host_prose(answer: str, fallback_title: str) -> "tuple[str, str]":
    """A host answer as ``(title, body)``: one reading, whichever host gave it."""
    title, body = split_title(answer)
    return title or fallback_title, _ingest.clip_body(body, by="summarise")


def _from_answer(
    session: str, prompts: "list[dict[str, Any]]", answer: str, label: str
) -> "tuple[str, str, str, Optional[str], Optional[str], int]":
    """:func:`_compose`'s result for an answer the caller already obtained.

    Counted as one call whatever it holds: the caller spent a request on the
    person's quota to get it, as a failed CLI call does.
    """
    fallback_title, fallback_body = structural_summary(session, prompts)
    if not answer.strip():
        return (
            fallback_title, fallback_body, "structural", None,
            f"the answer from {label} was empty", 1,
        )
    title, body = _host_prose(answer, fallback_title)
    return title, body, "host-agent", label, None, 1


def hands_off(*, use_host: bool, hand_off: Optional[str]) -> bool:
    """Whether pending sessions go to the caller instead of being summarised here.

    Never when the caller asked for structural, and never inside a
    summarisation: the depth cap outranks a caller's offer, since that caller
    could be the host process this module started.
    """
    if not use_host or not hand_off or os.environ.get(SUMMARISE_MARKER):
        return False
    return hand_off == "always" or not available_hosts()


def awaiting(
    repo_root: Path, *, exclude: "Iterable[str]" = ()
) -> "list[dict[str, Any]]":
    """The sessions :func:`summarise_pending` would take, and nothing written.

    Same selection, same cap: a caller summarising them spends the same quota
    per run as the CLI path would.
    """
    path = _store.db_path(repo_root, create=False)
    with _store.MemoryStore(path) as memory:
        sessions = memory.unsummarised_sessions(
            source_type=PROMPT_DOC_TYPE,
            summary_type=SESSION_DOC_TYPE,
            min_rows=MIN_PROMPTS,
            exclude=list(exclude),
            limit=MAX_PENDING,
        )
        return [
            {"session": session,
             "prompts": memory.session_count(session, doc_type=PROMPT_DOC_TYPE)}
            for session in sessions
        ]


def _compose(
    session: str,
    prompts: "list[dict[str, Any]]",
    replies: "Optional[list[Optional[str]]]" = None,
    *,
    use_host: bool,
) -> "tuple[str, str, str, Optional[Host], Optional[str], int]":
    """``(title, body, summary_source, host, fallback_reason, host_calls)``.

    Every path that does not produce host-agent prose lands on the structural
    summary and says why. There is no retry: T07 is explicit that a guard trip
    degrades for that invocation rather than trying again, and a retry inside a
    detached child is a second inference call nobody asked for.
    """
    fallback_title, fallback_body = structural_summary(session, prompts)
    if not use_host:
        return fallback_title, fallback_body, "structural", None, "--no-host-agent", 0
    if os.environ.get(SUMMARISE_MARKER):
        return (
            fallback_title, fallback_body, "structural", None,
            "a summarisation is already running in this process tree", 0,
        )
    hosts = available_hosts()
    if not hosts:
        return (
            fallback_title, fallback_body, "structural", None,
            "no host agent CLI on PATH ("
            + ", ".join(host.binary for host in HOSTS)
            + ")",
            0,
        )
    text = brief(prompts, replies)
    attempts = 0
    for host in hosts:
        attempts += 1
        answer = call_host(host, text)
        if answer is None:
            continue
        title, body = _host_prose(answer, fallback_title)
        return (
            title,
            body,
            "host-agent",
            host,
            None,
            attempts,
        )
    return (
        fallback_title, fallback_body, "structural", None,
        "every installed host agent failed or timed out", attempts,
    )
