"""When a session has finished: its last message, not its log file's mtime.

Found on the Remote SSH VM (VS Code 1.138, 2026-09-30): the last prompt of a
Chat session was sent at 22:07, the window was reloaded at 22:16, and a
user-initiated ``mem sync --summarise --hand-off always`` at 22:39 handed
nothing over. VS Code rewrites ``chatSessions/<id>.jsonl`` for UI state — a
kind-1 patch to ``inputState`` and the like — with no new message, the
companion re-copies the file whenever it changes, and sync judged "still
active" by the file's mtime. Each reload restarted the thirty minutes.

The shapes below were read off real logs on this Mac on 2026-09-30:
``chatSessions`` requests carry ``timestamp``, ``responseTimestamp`` and
``modelState.completedAt`` (epoch ms), and Copilot's ``transcripts/`` and the
CLI's ``events.jsonl`` carry an ISO ``timestamp`` on every event.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cartograph import hook
from cartograph.mem import store, summarise, sync

MIN = 60


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (hook.REENTRY_MARKER, summarise.SUMMARISE_MARKER, "COPILOT_CLI"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


@pytest.fixture()
def no_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(summarise.shutil, "which", lambda binary: None)


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _iso_ms(epoch: float) -> str:
    """The shape the logs write: milliseconds and a Z."""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _workspace(user_dir: Path, folder: Path) -> Path:
    ws = user_dir / "workspaceStorage" / "ws1"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "workspace.json").write_text(
        json.dumps({"folder": folder.resolve().as_uri()}), encoding="utf-8")
    return ws


def _chat_session(user_dir: Path, folder: Path, session: str, asked_at: list[float]) -> Path:
    """A chatSessions log whose requests were sent at *asked_at*, answered 5 s
    later, then patched for UI state only — and written just now."""
    ws = _workspace(user_dir, folder)
    (ws / "chatSessions").mkdir(exist_ok=True)

    def request(n: int, at: float) -> dict:
        ms = int(at * 1000)
        return {"requestId": f"request_{n}", "timestamp": ms, "responseTimestamp": ms,
                "modelState": {"value": 0},
                "message": {"text": f"Prompt number {n} of this chat", "parts": []},
                "response": [], "hiddenFromTranscript": False}

    lines: list[dict] = [{"kind": 0, "v": {
        "version": 3, "sessionId": session, "creationDate": int(asked_at[0] * 1000),
        "inputState": {"inputText": ""},
        "requests": [request(n, at) for n, at in enumerate(asked_at)],
    }}]
    for n, at in enumerate(asked_at):
        lines.append({"kind": 1, "k": ["requests", n, "modelState"],
                      "v": {"value": 1, "completedAt": int((at + 5) * 1000)}})
    # What a reload or a click into the chat writes: no message at all.
    lines.append({"kind": 1, "k": ["inputState", "inputText"], "v": "half-typed"})
    lines.append({"kind": 1, "k": ["inputState", "selections"], "v": []})
    path = ws / "chatSessions" / f"{session}.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _events(path: Path, session: str, turns: list[float], stamp, *,
            extra: list[dict] = (), cli_root: Path | None = None) -> Path:
    """An event log (transcript or CLI) with one prompt and reply per turn."""
    def event(kind: str, data: dict, at: float | None) -> dict:
        line = {"type": kind, "data": data, "id": f"{kind}-{at}", "parentId": None}
        if at is not None and stamp is not None:
            line["timestamp"] = stamp(at)
        return line

    start = {"sessionId": session, "version": 1, "producer": "copilot-agent"}
    if cli_root is not None:
        start["context"] = {"cwd": str(cli_root), "gitRoot": str(cli_root)}
    lines = [event("session.start", start, turns[0] if turns else time.time())]
    for n, at in enumerate(turns):
        lines += [
            event("user.message", {"content": f"Prompt number {n} of this session"}, at),
            event("assistant.turn_start", {"turnId": str(n)}, at + 1),
            event("assistant.message", {"content": f"reply {n}"}, at + 4),
            event("assistant.turn_end", {"turnId": str(n)}, at + 5),
        ]
    lines += list(extra)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _transcript(user_dir: Path, folder: Path, session: str, turns: list[float], stamp) -> Path:
    ws = _workspace(user_dir, folder)
    return _events(ws / "GitHub.copilot-chat" / "transcripts" / f"{session}.jsonl",
                   session, turns, stamp)


def _cli(copilot_dir: Path, root: Path, session: str, turns: list[float], stamp,
         extra: list[dict] = ()) -> Path:
    return _events(copilot_dir / "session-state" / session / "events.jsonl",
                   session, turns, stamp, extra=extra, cli_root=root)


def _sync(repo: Path, tmp_path: Path, **kw):
    return sync.sync(repo, user_dirs=[tmp_path / "User"], copilot_dir=tmp_path / "copilot",
                     summarise_sessions=True, **kw)


def _summaries(repo: Path) -> list[str]:
    with store.MemoryStore(store.db_path(repo, create=False)) as memory:
        return [row[0] for row in memory._conn.execute(  # noqa: SLF001
            "SELECT session FROM observations WHERE doc_type = 'sessions'")]


def _epoch_ms(at: float) -> int:
    return int(at * 1000)


# --- the bug, as it happened ---------------------------------------------------


def test_a_ui_rewrite_does_not_hold_back_a_finished_chat(repo, tmp_path, no_cli):
    """Last request 40 minutes ago, file written just now: handed over."""
    now = time.time()
    _chat_session(tmp_path / "User", repo, "f2f6", [now - 45 * MIN, now - 40 * MIN])

    result = _sync(repo, tmp_path, hand_off="always")

    assert [a["session"] for a in result["awaiting_summary"]] == ["f2f6"]
    assert result["waiting"] == []


def test_a_ui_rewrite_does_not_hold_back_the_engines_summary(repo, tmp_path):
    now = time.time()
    _chat_session(tmp_path / "User", repo, "f2f6", [now - 45 * MIN, now - 40 * MIN])

    result = _sync(repo, tmp_path, use_host=False)

    assert [r["session"] for r in result["summarised"]] == ["f2f6"]
    assert _summaries(repo) == ["f2f6"]


def test_the_mirror_counts_the_same_way(repo, tmp_path, no_cli):
    """Remote SSH: the companion's copy is written whenever VS Code's is."""
    now = time.time()
    source = _chat_session(tmp_path / "local", repo, "f2f6", [now - 45 * MIN, now - 40 * MIN])
    mirror = sync.mirror_dir(repo)
    mirror.mkdir(parents=True)
    (mirror / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    result = _sync(repo, tmp_path, hand_off="always")

    assert [a["session"] for a in result["awaiting_summary"]] == ["f2f6"]


# --- what is waiting, and until when -------------------------------------------


def test_a_recent_chat_waits_and_says_until_when(repo, tmp_path, no_cli):
    now = time.time()
    last = now - 5 * MIN
    _chat_session(tmp_path / "User", repo, "live", [now - 10 * MIN, last])

    result = _sync(repo, tmp_path, hand_off="always")

    assert result["awaiting_summary"] == []
    # The reply completed 5 s after the request: that is the last message.
    assert result["waiting"] == [{
        "session": "live",
        "last_message_at": _iso(last + 5),
        "settles_at": _iso(last + 5 + sync.SETTLE_SECONDS),
    }]
    assert _summaries(repo) == []


def test_a_session_that_is_not_waiting_to_settle_is_not_listed(repo, tmp_path, no_cli):
    """One prompt is below the summary threshold: not waiting on anything."""
    now = time.time()
    _chat_session(tmp_path / "User", repo, "short", [now - 5 * MIN])

    result = _sync(repo, tmp_path, hand_off="always")

    assert result["waiting"] == []


def test_the_latest_message_across_a_sessions_logs_decides(repo, tmp_path, no_cli):
    """The chat store's last request is old, but its transcript has a newer turn."""
    now = time.time()
    user = tmp_path / "User"
    _chat_session(user, repo, "s1", [now - 50 * MIN, now - 45 * MIN])
    _transcript(user, repo, "s1", [now - 50 * MIN, now - 3 * MIN], _iso_ms)

    result = _sync(repo, tmp_path, hand_off="always")

    assert result["awaiting_summary"] == []
    assert [w["session"] for w in result["waiting"]] == ["s1"]
    assert result["waiting"][0]["last_message_at"] == _iso(now - 3 * MIN + 5)


# --- transcripts and the CLI: ISO and epoch timestamps --------------------------


@pytest.mark.parametrize("stamp", [_iso_ms, _epoch_ms], ids=["iso", "epoch-ms"])
def test_an_old_transcript_rewritten_now_is_settled(repo, tmp_path, no_cli, stamp):
    now = time.time()
    _transcript(tmp_path / "User", repo, "t1", [now - 50 * MIN, now - 40 * MIN], stamp)

    result = _sync(repo, tmp_path, hand_off="always")

    assert [a["session"] for a in result["awaiting_summary"]] == ["t1"]


@pytest.mark.parametrize("stamp", [_iso_ms, _epoch_ms], ids=["iso", "epoch-ms"])
def test_a_recent_transcript_waits(repo, tmp_path, no_cli, stamp):
    now = time.time()
    last = now - 5 * MIN
    _transcript(tmp_path / "User", repo, "t1", [now - 10 * MIN, last], stamp)

    result = _sync(repo, tmp_path, hand_off="always")

    assert result["awaiting_summary"] == []
    assert result["waiting"] == [{
        "session": "t1",
        "last_message_at": _iso(last + 5),
        "settles_at": _iso(last + 5 + sync.SETTLE_SECONDS),
    }]


@pytest.mark.parametrize("stamp", [_iso_ms, _epoch_ms], ids=["iso", "epoch-ms"])
def test_cli_events_settle_by_their_last_message(repo, tmp_path, no_cli, stamp):
    """A later non-message event — a resume, a hook — is not a message."""
    now = time.time()
    copilot = tmp_path / "copilot"
    later = [{"type": "session.resume", "data": {}, "timestamp": stamp(now - 1 * MIN)},
             {"type": "hook.start", "data": {"hookType": "sessionStart"},
              "timestamp": stamp(now - 1 * MIN)}]
    _cli(copilot, repo, "done", [now - 50 * MIN, now - 40 * MIN], stamp, extra=later)
    _cli(copilot, repo, "live", [now - 10 * MIN, now - 5 * MIN], stamp)

    result = _sync(repo, tmp_path, hand_off="always")

    assert [a["session"] for a in result["awaiting_summary"]] == ["done"]
    assert [(w["session"], w["last_message_at"]) for w in result["waiting"]] == [
        ("live", _iso(now - 5 * MIN + 5))
    ]


# --- no timestamps at all ------------------------------------------------------


def test_a_log_without_timestamps_falls_back_to_mtime_and_says_so(repo, tmp_path, no_cli):
    now = time.time()
    path = _transcript(tmp_path / "User", repo, "bare", [now - 50 * MIN, now - 40 * MIN],
                       stamp=None)
    written = now - 2 * MIN
    os.utime(path, (written, written))

    result = _sync(repo, tmp_path, hand_off="always")

    assert result["awaiting_summary"] == []
    [waiting] = result["waiting"]
    assert waiting["session"] == "bare"
    assert waiting["last_message_at"] == _iso(written)
    assert waiting["settles_at"] == _iso(written + sync.SETTLE_SECONDS)
    assert "modification time" in waiting["reason"]
    assert "bare.jsonl" in waiting["reason"]


def test_a_log_without_timestamps_settles_by_mtime(repo, tmp_path, no_cli):
    now = time.time()
    path = _transcript(tmp_path / "User", repo, "bare", [now - 50 * MIN, now - 40 * MIN],
                       stamp=None)
    old = now - sync.SETTLE_SECONDS - MIN
    os.utime(path, (old, old))

    result = _sync(repo, tmp_path, hand_off="always")

    assert [a["session"] for a in result["awaiting_summary"]] == ["bare"]


# --- unchanged elsewhere --------------------------------------------------------


def test_a_second_sync_of_an_unchanged_log_keeps_its_answer(repo, tmp_path, no_cli):
    """The import skips an unchanged file; the settle time must not vanish with it."""
    now = time.time()
    _chat_session(tmp_path / "User", repo, "live", [now - 10 * MIN, now - 5 * MIN])
    first = _sync(repo, tmp_path, hand_off="always")

    again = _sync(repo, tmp_path, hand_off="always")

    assert again["waiting"] == first["waiting"]
    assert [w["session"] for w in again["waiting"]] == ["live"]
    assert "reason" not in again["waiting"][0]
    assert again["awaiting_summary"] == []


def test_a_new_message_after_a_sync_restarts_the_wait(repo, tmp_path, no_cli):
    """The per-file cache must follow the file: a chat picked up again after
    forty minutes is live again, not settled from the first read."""
    now = time.time()
    user = tmp_path / "User"
    _chat_session(user, repo, "back", [now - 45 * MIN, now - 40 * MIN])
    _sync(repo, tmp_path, hand_off="always")  # creates the store
    assert [a["session"] for a in _sync(repo, tmp_path, hand_off="always")["awaiting_summary"]] \
        == ["back"]

    _chat_session(user, repo, "back", [now - 45 * MIN, now - 40 * MIN, now - 1 * MIN])
    result = _sync(repo, tmp_path, hand_off="always")

    assert result["awaiting_summary"] == []
    assert [w["session"] for w in result["waiting"]] == ["back"]
