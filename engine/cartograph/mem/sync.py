"""Memory from the logs Copilot writes itself, for when hooks never ran.

Hooks are the primary capture path and an organisation can switch them off.
On the first Linux test machine ``chat.useHooks`` was disabled by policy: the
hook worked when run by hand, VS Code never ran it, and nothing said so.
The conversations are logged either way — Chat per workspace, by VS Code in
``workspaceStorage/<id>/chatSessions/<session>.jsonl`` and by Copilot in
``…/GitHub.copilot-chat/transcripts/<session>.jsonl``; the CLI globally under
``~/.copilot/session-state/<session>/events.jsonl`` — and this module imports
the prompts from them.

It is a reconciliation, not a second capture path. Session ids in the logs are
the ids the hooks receive, so :meth:`MemoryStore.has_document` makes a prompt
a hook already recorded a no-op here, and the count of prompts that were *not*
already recorded is the evidence of whether hooks are firing at all. That
evidence is stored and reported by ``carto mem status``, because a fallback
nobody can see is the failure this exists to fix.

Only prompts are imported. The replies are in the same files and would make
better summaries; that is a separate change, since a reply is not a prompt and
must not be searchable as one.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional
from urllib.parse import unquote, urlparse

from . import ingest as _ingest
from . import store as _store

logger = logging.getLogger(__name__)

CHAT_HOST = "copilot-chat"
CLI_HOST = "copilot-cli"

#: How long a session's log must be unchanged before an import will summarise
#: it. A log gives no end-of-session signal, and a session is summarised only
#: once, ever — summarising one someone is still typing into would spend that
#: once on half of it. Thirty minutes is past any pause inside one task.
SETTLE_SECONDS = 30 * 60

#: ``mem_meta`` keys. Per-file size and mtime, so an unchanged log is not
#: reparsed on every sync; and the last result, for ``mem status``.
_FILE_KEY = "sync:file:"
_STATUS_KEY = "sync:status"
#: Sessions a sync has recorded prompts for. A session hooks also captured
#: would still be marked — the mark answers "did the logs carry any of this",
#: which is the question ``capture`` is asking.
_FROM_LOGS_KEY = "sync:from-logs:"


# ---------------------------------------------------------------------------
# Where the logs are
# ---------------------------------------------------------------------------


def vscode_user_dirs() -> list[Path]:
    """VS Code ``User`` directories on this machine that exist.

    The remote server's is first: under Remote SSH the extension, the engine
    and Copilot Chat all run on the remote, and so do its logs.
    """
    home = Path.home()
    candidates = [
        home / ".vscode-server" / "data" / "User",
        home / ".vscode-server-insiders" / "data" / "User",
    ]
    if sys.platform == "darwin":
        support = home / "Library" / "Application Support"
        candidates += [support / "Code" / "User", support / "Code - Insiders" / "User"]
    elif sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        candidates += [appdata / "Code" / "User", appdata / "Code - Insiders" / "User"]
    else:
        config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        candidates += [config / "Code" / "User", config / "Code - Insiders" / "User"]
    return [path for path in candidates if path.is_dir()]


def _uri_path(uri: str) -> Optional[Path]:
    """The filesystem path a workspace URI names, whatever its scheme.

    ``file:///root/x`` locally, ``vscode-remote://ssh-remote+host/root/x`` on a
    remote server — the path component is the folder in both.
    """
    try:
        path = unquote(urlparse(uri).path)
    except ValueError:
        return None
    if not path:
        return None
    # file:///c%3A/Users/x on Windows parses to /c:/Users/x.
    if len(path) > 2 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return Path(path)


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def chat_logs(repo_root: Path, user_dirs: Iterable[Path]) -> list[Path]:
    """Chat logs for workspaces opened on *repo_root* or inside it.

    Two per session, from two owners. ``chatSessions/`` is VS Code's own store,
    the one it restores chats from, and it held every prompt with hooks off.
    Copilot's ``transcripts/`` did not: measured with ``chat.useHooks`` off,
    it held ``session.start`` and nothing else. Both are read — they share
    session ids, so a prompt in both is recorded once.
    """
    found: list[Path] = []
    for user_dir in user_dirs:
        for meta in sorted((user_dir / "workspaceStorage").glob("*/workspace.json")):
            try:
                folder = json.loads(meta.read_text(encoding="utf-8")).get("folder")
            except (OSError, ValueError):
                continue
            # A multi-root workspace has "workspace", not "folder", and no one
            # folder its chats belong to.
            path = _uri_path(folder) if isinstance(folder, str) else None
            if path is None or not _within(path, repo_root):
                continue
            found += sorted((meta.parent / "chatSessions").glob("*.jsonl"))
            transcripts = meta.parent / "GitHub.copilot-chat" / "transcripts"
            found += sorted(transcripts.glob("*.jsonl"))
    return found + mirrored_chat_logs(repo_root)


def workspace_chat_logs(workspace: Path) -> list[Path]:
    """Both chat logs in one named ``workspaceStorage/<id>/`` directory.

    For the extension, which knows its own. On a remote, that directory holds
    Copilot's transcripts but no ``workspace.json`` — measured on the Remote
    SSH VM — so :func:`chat_logs` could never match it by folder.
    """
    return sorted((workspace / "chatSessions").glob("*.jsonl")) + sorted(
        (workspace / "GitHub.copilot-chat" / "transcripts").glob("*.jsonl")
    )


def mirror_dir(repo_root: Path) -> Path:
    """Where the companion extension copies chat files in a remote window.

    Over Remote SSH, Dev Containers or WSL, VS Code keeps ``chatSessions/`` on
    the machine the window runs on, and the engine runs on the other one —
    measured: the Ubuntu VM had none, the Windows host had the chat. The
    Windows-side companion copies each file here, beside the memory store, in
    the directory ``carto install`` already keeps out of git.
    """
    return _store.db_path(repo_root, create=False).parent / "chatSessions"


def mirrored_chat_logs(repo_root: Path) -> list[Path]:
    return sorted(mirror_dir(repo_root).glob("*.jsonl"))


def cli_logs(repo_root: Path, copilot_dir: Path) -> list[Path]:
    """Copilot CLI session logs whose session started inside *repo_root*.

    The summariser runs ``copilot -p`` in the temp directory, so its own
    sessions fail this test and are never imported as a person's prompts.
    """
    found: list[Path] = []
    for events in sorted((copilot_dir / "session-state").glob("*/events.jsonl")):
        start = next(_events(events), None)
        if not start or start.get("type") != "session.start":
            continue
        context = (start.get("data") or {}).get("context") or {}
        where = context.get("gitRoot") or context.get("cwd")
        if isinstance(where, str) and _within(Path(where), repo_root):
            found.append(events)
    return found


def _events(path: Path) -> Iterator[dict[str, Any]]:
    """Each complete JSON line. A half-written last line is skipped, not fatal:
    the host may be writing the file at this moment."""
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    yield event
    except OSError:
        return


# ---------------------------------------------------------------------------
# The import
# ---------------------------------------------------------------------------


def _replay(path: Path) -> dict[str, Any]:
    """A ``chatSessions`` log replayed into the session it describes.

    Line one is a snapshot (``kind`` 0, the state in ``v``); each later line
    patches it — ``kind`` 1 sets ``v`` at the path ``k``, ``kind`` 2 appends
    the items in ``v`` to the list at ``k``. A patch whose path no longer
    resolves is skipped rather than guessed at.
    """
    state: dict[str, Any] = {}
    for line in _events(path):
        kind, keys, value = line.get("kind"), line.get("k") or [], line.get("v")
        if kind == 0 and isinstance(value, dict):
            state = value
            continue
        if not keys:
            continue
        target: Any = state
        try:
            for key in keys[:-1]:
                target = target[key]
            if kind == 1:
                target[keys[-1]] = value
            elif kind == 2 and isinstance(value, list):
                target[keys[-1]].extend(value)
        except (KeyError, IndexError, TypeError, AttributeError):
            continue
    return state


#: One turn of a conversation: what the person asked, and the assistant's final
#: reply to it, or None when the log holds no reply (yet).
Exchange = tuple[str, Optional[str]]


def _exchanges(path: Path) -> tuple[Optional[str], list[Exchange], bool]:
    """``(session, exchanges, hooks_fired)`` from one log.

    The reply is the turn's *final* assistant text — what the turn concluded —
    not every intermediate message. claude-mem makes the same cut: its Stop
    hook reads only the last assistant message of a turn.
    """
    if path.parent.name == "chatSessions":
        state = _replay(path)
        turns: list[Exchange] = []
        for request in state.get("requests") or []:
            if not isinstance(request, dict) or request.get("hiddenFromTranscript"):
                continue
            text = (request.get("message") or {}).get("text")
            if not isinstance(text, str):
                continue
            # A response is a list of parts; the prose ones carry `value`.
            reply = "".join(
                part["value"] for part in request.get("response") or []
                if isinstance(part, dict) and isinstance(part.get("value"), str)
            ).strip()
            turns.append((text, reply or None))
        return state.get("sessionId") or path.stem, turns, False

    session: Optional[str] = None
    turns = []
    hooks_fired = False
    for event in _events(path):
        kind, data = event.get("type"), event.get("data") or {}
        if kind == "session.start":
            session = data.get("sessionId") or session
        elif kind == "user.message" and isinstance(data.get("content"), str):
            turns.append((data["content"], None))
        elif kind == "assistant.message" and turns and isinstance(data.get("content"), str):
            if data["content"].strip():
                turns[-1] = (turns[-1][0], data["content"].strip())
        elif kind == "hook.start" and data.get("hookType") in {
            "userPromptSubmitted", "UserPromptSubmit",
        }:
            hooks_fired = True
    if session is None:
        # A Chat transcript is named for its session; a CLI log's directory is.
        session = path.parent.name if path.name == "events.jsonl" else path.stem
    return session, turns, hooks_fired


def _read(path: Path) -> tuple[Optional[str], list[str], bool]:
    """``(session, prompts, hooks_fired)`` from one log."""
    session, turns, hooks_fired = _exchanges(path)
    return session, [prompt for prompt, _ in turns], hooks_fired


def session_exchanges(
    repo_root: Path,
    session: str,
    *,
    user_dirs: Optional[Iterable[Path]] = None,
    workspace_dirs: Iterable[Path] = (),
    copilot_dir: Optional[Path] = None,
) -> list[Exchange]:
    """One session's turns, from the most complete log that has them.

    VS Code's ``chatSessions`` held every prompt and reply with hooks off; its
    transcript did not; the CLI has one log. So the log with the most replies
    wins, and an empty list means no log for this session is reachable here.
    """
    chat = chat_logs(repo_root, vscode_user_dirs() if user_dirs is None else user_dirs)
    for workspace in workspace_dirs:
        chat += workspace_chat_logs(workspace)
    candidates = [log for log in chat if log.stem == session] + [
        log for log in cli_logs(repo_root, copilot_dir or Path.home() / ".copilot")
        if log.parent.name == session
    ]
    best: list[Exchange] = []
    for log in candidates:
        _, turns, _ = _exchanges(log)
        if sum(1 for _, reply in turns if reply) > sum(1 for _, reply in best if reply) or (
            not best and turns
        ):
            best = turns
    return best


def _stamp(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _capture_mode(stats: dict[str, Any]) -> str:
    """What the numbers say about how prompts are reaching the store.

    A prompt already recorded counts for hooks only if an earlier sync did not
    record it: measured on the Remote SSH VM, a file that changed two minutes
    after its import was otherwise reported as ``hooks``.
    """
    if stats["imported"] or stats["recorded_from_logs"]:
        return "logs"
    if stats["already_recorded"]:
        return "hooks"
    return "unknown"


def sync(
    repo_root: Path,
    *,
    user_dirs: Optional[Iterable[Path]] = None,
    workspace_dirs: Iterable[Path] = (),
    copilot_dir: Optional[Path] = None,
    project: Optional[str] = None,
    summarise_sessions: bool = False,
    use_host: bool = True,
    hand_off: Optional[str] = None,
) -> dict[str, Any]:
    """Import the prompts Copilot logged for *repo_root* that are not stored yet.

    Never creates a store unless there is a prompt to put in it, so running it
    on a repository nobody has chatted in leaves nothing behind.
    """
    chat = chat_logs(repo_root, vscode_user_dirs() if user_dirs is None else user_dirs)
    for workspace in workspace_dirs:
        chat += workspace_chat_logs(workspace)
    sources = {
        # dict.fromkeys: a directory named explicitly may also have matched.
        CHAT_HOST: list(dict.fromkeys(chat)),
        CLI_HOST: cli_logs(repo_root, copilot_dir or Path.home() / ".copilot"),
    }
    hosts: dict[str, dict[str, Any]] = {}
    changing: list[str] = []
    path = _store.db_path(repo_root, create=False)
    memory: Optional[_store.MemoryStore] = None
    embedded = 0
    try:
        if path.exists():
            memory = _store.MemoryStore(path)
        for host, logs in sources.items():
            stats: dict[str, Any] = {
                "files": len(logs), "prompts_seen": 0, "already_recorded": 0,
                "recorded_from_logs": 0, "imported": 0,
            }
            if host == CLI_HOST:
                stats["hooks_fired"] = False
            for log in logs:
                if time.time() - log.stat().st_mtime < SETTLE_SECONDS:
                    changing.append(log.stem if host == CHAT_HOST else log.parent.name)
                if memory is not None and memory.get_meta(_FILE_KEY + str(log)) == _stamp(log):
                    continue
                session, prompts, hooks_fired = _read(log)
                if host == CLI_HOST:
                    stats["hooks_fired"] = stats["hooks_fired"] or hooks_fired
                for text in prompts:
                    shaped = _ingest.observation_from({"session_id": session, "prompt": text})
                    if shaped is None:
                        continue
                    stats["prompts_seen"] += 1
                    if memory is None:
                        memory = _store.MemoryStore(_store.db_path(repo_root, create=True))
                    if memory.has_document(session, shaped["body"], doc_type=shaped["doc_type"]):
                        stats["already_recorded"] += 1
                        if memory.get_meta(_FROM_LOGS_KEY + session):
                            stats["recorded_from_logs"] += 1
                        continue
                    if memory.session_count(session) >= _ingest.SESSION_CAP:
                        continue
                    memory.add(
                        project=project or repo_root.name, platform_source=host,
                        embed=False, **shaped,
                    )
                    memory.set_meta(_FROM_LOGS_KEY + session, "1")
                    stats["imported"] += 1
                if memory is not None:
                    memory.set_meta(_FILE_KEY + str(log), _stamp(log))
            stats["capture"] = _capture_mode(stats)
            hosts[host] = stats
        if memory is not None:
            previous = last_status(memory)
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            for host, stats in hosts.items():
                # A run that read nothing new says nothing about the hooks; keep
                # the last run that did.
                if stats["prompts_seen"] or host not in previous:
                    previous[host] = {**stats, "at": now}
            memory.set_meta(_STATUS_KEY, json.dumps(previous))
            # Once, after the import, and without a bound: sync is the command
            # that runs off the host's turn (the extension's timer, a person),
            # so it is where rows the hooks captured get their vectors, and
            # where an older store is backfilled in full.
            embedded = memory.embed_missing()
    finally:
        if memory is not None:
            memory.close()

    imported = sum(stats["imported"] for stats in hosts.values())
    result: dict[str, Any] = {
        "summary": f"Imported {imported} prompt(s) from Copilot's logs.",
        "hosts": hosts,
        "embedded": embedded,
    }
    if summarise_sessions:
        from . import summarise as _summarise

        result["summarised"] = []
        if _summarise.hands_off(use_host=use_host, hand_off=hand_off):
            # The caller said it can summarise and no CLI here will: name the
            # sessions and write nothing, so each keeps its one summary for
            # whatever the caller manages — or structural, if it cannot.
            result["awaiting_summary"] = (
                _summarise.awaiting(repo_root, exclude=changing) if path.exists() else []
            )
        elif path.exists():
            result["summarised"] = [
                outcome
                for outcome in _summarise.summarise_pending(
                    repo_root, exclude=changing, project=project, use_host=use_host,
                    user_dirs=user_dirs, workspace_dirs=workspace_dirs,
                )
                if outcome.get("observation")
            ]
    return result


def last_status(memory: _store.MemoryStore) -> dict[str, Any]:
    """The per-host outcome of the last import that saw a prompt, or {}."""
    raw = memory.get_meta(_STATUS_KEY)
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}
