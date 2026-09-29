"""``carto hook <event>`` — the entry point a host invokes.

Every other command in Cartograph answers an agent. This one answers a *host*,
and the two protocols differ in the place it matters most. On the query path an
exit code of ``2`` means "precondition unmet, here is the remediation" (see
:mod:`cartograph.envelope`). In a hook it means *blocking feedback to the
model*: the host stops what the model was doing and hands it stderr. A missing
graph exiting ``2`` would therefore halt a session over something the agent
never asked for — worse than the hook not existing.

So nothing here is routed through the envelope helpers, and nothing here exits
non-zero. A hook that cannot do its job says nothing and gets out of the way.

Copilot's two schemas spell the same moment differently — ``SessionStart``
and ``sessionStart``, ``SessionEnd`` and ``sessionEnd``. The events below are
therefore named after the *job*, with the host spellings as aliases, so the
mapping lives in Python where it can be tested rather than in a command string.

Two events read the host's stdin payload: ``prompt-capture``, for the text, and
``session-summarise``, for the session id. Neither waits for what it starts —
``session-summarise`` in particular hands its work to :func:`spawn_detached`,
because a summarisation is an inference call and Copilot has no async hook type
at all.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

#: Set in the environment of anything a hook launches, and checked before a
#: hook does anything at all. Without it the cycle is short: a hook starts an
#: update, the update shells out to the host agent for summarisation, that
#: agent starts a session, the session fires SessionStart. The child inherits
#: the marker, so one check covers the whole process tree a hook created.
REENTRY_MARKER = "CARTO_HOOK_ACTIVE"

#: Windows process-creation flags. Read through ``getattr`` so the constants
#: are addressable on POSIX too — otherwise the Windows branch of
#: :func:`_detach_kwargs` could only be exercised on Windows, which is the one
#: platform where getting it wrong has already cost an upstream defect.
_DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

ABSENT, STALE, READY = "absent", "stale", "ready"

#: One line per state, and the line is the entire SessionStart output. It
#: orients the agent and stops: the command reference belongs to
#: ``carto capabilities`` and the workflows belong to the skills pack, and
#: restating either here is a token tax paid on every session for the life of
#: the install. Each names at most one command — a pointer, not a catalogue.
_SESSION_LINES = {
    READY: (
        "[carto] Graph ready — prefer the cartograph skills to reading files "
        "cold; `carto capabilities` lists the rest."
    ),
    STALE: (
        "[carto] Graph is stale (built on another branch) — run `carto update` "
        "before relying on it."
    ),
    ABSENT: (
        "[carto] No graph yet — run `carto build`; until it exists the "
        "cartograph skills fall back to plain search."
    ),
}


def _detach_kwargs(platform: str = sys.platform) -> dict[str, Any]:
    """The platform-specific half of :func:`spawn_detached`.

    Split out so both branches can be asserted from either platform.
    """
    if platform == "win32":
        # DETACHED_PROCESS gives up the console the host handed us;
        # CREATE_NEW_PROCESS_GROUP stops a Ctrl-C aimed at the host from
        # travelling down into the build.
        return {"creationflags": _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP}
    # setsid. A new session, so the child outlives the process group the host
    # tears down when it reaps the hook.
    return {"start_new_session": True}


def spawn_detached(argv: Sequence[str], *, cwd: Optional[Path] = None) -> bool:
    """Start *argv* so that it outlives this process, on POSIX and on Windows.

    One function, shared by every caller, because the two halves are not
    interchangeable and the wrong one fails invisibly. Trailing ``&`` is the
    reflex and it is upstream defect #5: on Windows it backgrounds nothing, the
    child stays attached, and the host kills it when the hook times out — 30
    seconds in Copilot, which has no async hook type at all, against a build
    that takes minutes.

    Detaching is only half of it. The child must also let go of the hook's
    stdio: a host that waits for EOF on the hook's stdout waits for the child
    too, and then the detach has bought nothing.

    Returns whether the process started. Failure is not reported anywhere else
    — there is nothing the host could do about it, and nothing the model needs
    to hear.
    """
    env = dict(os.environ)
    env[REENTRY_MARKER] = "1"
    try:
        subprocess.Popen(  # noqa: S603 — argv is built here, never user text
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **_detach_kwargs(),
        )
    except OSError:
        return False
    return True


def graph_state(repo_root: Path) -> str:
    """``absent``, ``stale`` or ``ready`` — the whole input the session line needs.

    Read directly rather than through ``GraphStore``, which creates the data
    directory and runs migrations when it opens. A hook that reports on the
    graph must not be the thing that brings one into existence.
    """
    from .incremental import get_db_path

    db_path = get_db_path(repo_root, read_only=True)
    if not db_path.exists():
        return ABSENT
    return STALE if _built_on_another_branch(repo_root, db_path) else READY


def _built_on_another_branch(repo_root: Path, db_path: Path) -> bool:
    """The same test ``carto status --format json`` reports as ``data.stale``."""
    try:
        conn = sqlite3.connect(str(db_path), timeout=2)
        try:
            row = conn.execute(
                "SELECT value FROM metadata WHERE key = 'git_branch'"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        # An unreadable graph is not a stale graph, and a hook is the wrong
        # place to start diagnosing the difference.
        return False
    built_on = row[0] if row else ""
    if not built_on:
        return False
    from .incremental import _git_branch_info

    current, _ = _git_branch_info(repo_root)
    return bool(current) and current != built_on


def self_argv(*args: str) -> list[str]:
    """*args* as a command line that re-enters this same Cartograph.

    Addressed through ``sys.executable`` rather than the ``carto`` script, so
    the child does not depend on what is on the host's PATH by the time it
    starts. Unfrozen that is an interpreter, reached with ``-m cartograph``. In
    a PyInstaller build it is the ``carto`` binary itself, which has no ``-m``:
    argparse reads ``cartograph`` as a subcommand, exits 1 behind DEVNULL, and
    the detached work silently never happens. That shipped once — every
    file-edit refresh in the v0.1.0 ``.vsix`` was a no-op — and it is the same
    defect class :data:`~cartograph.constants.GRAMMAR_PROBE_FLAG` exists for.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "cartograph", *args]


def update_argv(repo_root: Path) -> list[str]:
    """The refresh to run detached."""
    return self_argv("update", "--skip-flows", "--repo", str(repo_root))


def summarise_argv(repo_root: Path, session: str) -> list[str]:
    """The session summary to run detached.

    The session is passed explicitly rather than left to be re-derived: by the
    time a detached child starts, the latest prompt in the store may belong to
    whatever the person opened next.
    """
    return self_argv("mem", "summarise", "--session", session, "--repo", str(repo_root))


def catchup_argv(repo_root: Path, current_session: str) -> list[str]:
    """Summarise every earlier session that never got one, leaving *current_session*."""
    return self_argv(
        "mem", "summarise", "--pending",
        "--exclude-session", current_session, "--repo", str(repo_root),
    )


def resolve_host(host: Optional[str]) -> Optional[str]:
    """The host to record, with ``copilot`` narrowed to the one that ran the hook.

    Copilot CLI and Copilot Chat read the same ``.github/hooks`` file and send
    payloads of the same shape, so neither the file nor the payload can say
    which of them fired. The CLI sets ``COPILOT_CLI=1`` in every hook's
    environment and VS Code does not — read off both hosts on 2026-09-29, see
    ``docs/copilot-hooks.md``. That is the host stating it, not a guess from
    field names.
    """
    if host != "copilot":
        return host
    return "copilot-cli" if os.environ.get("COPILOT_CLI") else "copilot-chat"


def _session_status(repo_root: Path, _host: Optional[str] = None) -> int:
    """Emit the one orienting line for this session, once, on stdout."""
    print(_SESSION_LINES[graph_state(repo_root)])
    return 0


def hook_payload(stream: Any = None) -> Optional[dict]:
    """The JSON object the host pipes in, or None.

    ``isatty`` first, because a hook run by hand from a terminal has no
    payload coming and ``read()`` would block until someone typed EOF —
    indistinguishable, from the outside, from a hook that hangs the session.
    """
    stream = sys.stdin if stream is None else stream
    try:
        if stream is None or stream.isatty():
            return None
        payload = json.loads(stream.read())
    except (OSError, ValueError, AttributeError):
        # A host that sent nothing, or sent something that is not JSON, has
        # given this hook no work to do. That is not a condition to report.
        return None
    return payload if isinstance(payload, dict) else None


def _prompt_capture(repo_root: Path, host: Optional[str] = None) -> int:
    """Record the submitted prompt, in process.

    In process rather than through ``carto mem add``: the write is one SQLite
    insert, and a subprocess — interpreter start, package import, store open —
    would cost two orders of magnitude more than the work it wrapped.

    What is worth recording, and what is refused, is
    :mod:`cartograph.mem.ingest`'s decision, not this module's; the host
    protocol is all that lives here.
    """
    payload = hook_payload()
    if payload is None:
        return 0
    from .mem import ingest

    ingest.capture(repo_root, payload, host=host)
    return 0


def _session_summarise(repo_root: Path, _host: Optional[str] = None) -> int:
    """Launch the session summary and return without waiting for it.

    A summarisation is a process spawn plus an inference call — seconds at best,
    and the host that fires this event is closing a session and will not wait.
    So this does the same as ``file-update``: work out the argv, hand it to
    ``spawn_detached``, and return. Nothing here reads the answer, because there
    is no one left to tell.

    Two cheap refusals before the spawn. Without a session id there is nothing
    to summarise, and with no store there was never a captured prompt in this
    repository — spawning an interpreter to discover either would be work done
    for nothing on every session end.
    """
    payload = hook_payload()
    if payload is None:
        return 0
    from .mem import ingest
    from .mem.store import db_path

    session = ingest.session_from(payload)
    if not session or not db_path(repo_root, create=False).exists():
        return 0
    spawn_detached(summarise_argv(repo_root, session), cwd=repo_root)
    return 0


def _session_catchup(repo_root: Path, _host: Optional[str] = None) -> int:
    """Summarise, in the background, the sessions that ended without saying so.

    VS Code has no ``SessionEnd``: its ``Stop`` is a turn boundary, and a Chat
    session simply stops being used. The next session to *start* is therefore
    the first moment anything knows the earlier ones are over. It also catches
    a CLI session that crashed before its ``SessionEnd`` fired.

    The session starting now is named so it is left alone — it has only just
    begun. Prints nothing: VS Code reads a hook's stdout as JSON.
    """
    payload = hook_payload()
    if payload is None:
        return 0
    from .mem import ingest
    from .mem.store import db_path

    session = ingest.session_from(payload)
    if not session or not db_path(repo_root, create=False).exists():
        return 0
    spawn_detached(catchup_argv(repo_root, session), cwd=repo_root)
    return 0


def _file_update(repo_root: Path, _host: Optional[str] = None) -> int:
    """Launch the graph refresh and return without waiting for it.

    Returning immediately is not an optimisation. Copilot has no async hook
    type — every hook is synchronous, with a 30-second default timeout — and an
    update on a large repository takes minutes. Waiting would guarantee the
    host killed it part-way, every time.
    """
    spawn_detached(update_argv(repo_root), cwd=repo_root)
    return 0


_EVENTS: dict[str, Callable[[Path, Optional[str]], int]] = {
    "session-status": _session_status,
    "file-update": _file_update,
    "prompt-capture": _prompt_capture,
    "session-summarise": _session_summarise,
    "session-catchup": _session_catchup,
}

#: Host spellings for the three moments, keyed by their normalised form so
#: that SessionStart, sessionStart and session_start all arrive at the same
#: place.
_ALIASES = {
    "sessionstatus": "session-status",
    "sessionstart": "session-status",
    "fileupdate": "file-update",
    "filechanged": "file-update",
    "posttooluse": "file-update",
    "promptcapture": "prompt-capture",
    "userpromptsubmit": "prompt-capture",
    "promptsubmit": "prompt-capture",
    "userprompt": "prompt-capture",
    # Read off real payloads from this machine, not recalled. SessionEnd fires
    # once per session, and Copilot CLI's own schema spells it `sessionEnd`.
    # `Stop` is deliberately NOT here: it is a turn boundary, and aliasing it
    # would summarise the same session after every reply.
    "sessionend": "session-summarise",
    "sessionsummarise": "session-summarise",
    "sessioncatchup": "session-catchup",
}


def resolve_event(name: str) -> Optional[str]:
    """Map a host's spelling of an event onto one of :data:`_EVENTS`."""
    return _ALIASES.get("".join(c for c in name.lower() if c.isalnum()))


def _repo_root(repo: Optional[str]) -> Path:
    from .incremental import find_project_root

    if repo:
        return Path(repo).expanduser().resolve()
    return find_project_root(Path.cwd())


def run(event: str, repo: Optional[str] = None, host: Optional[str] = None) -> int:
    """Run one hook event. Always returns 0 — see the module docstring.

    The marker check covers capture as well as the spawning events, and has
    to: an observation written from inside a hook-launched session would be a
    record of Cartograph's own activity, indexed as if a person had asked for
    it.
    """
    if os.environ.get(REENTRY_MARKER):
        return 0
    resolved = resolve_event(event)
    if resolved is None:
        # A misconfigured host config is a problem for whoever wrote it, not
        # for the model. stderr is where a host logs; stdout is what it may
        # hand to the model, so the complaint goes to stderr.
        print(f"carto hook: unknown event {event!r}", file=sys.stderr)
        return 0
    try:
        return _EVENTS[resolved](_repo_root(repo), resolve_host(host))
    except Exception as exc:  # noqa: BLE001 — a hook must never be the thing that fails
        print(f"carto hook: {resolved} failed: {exc}", file=sys.stderr)
        return 0
