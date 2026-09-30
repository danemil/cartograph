"""Summaries written by a caller that is not the engine — decision 1.

Where ``copilot`` is not on PATH, the VS Code extension can still reach
Copilot's models through ``vscode.lm``. The engine's part is small and has to
be exact: say which sessions await a summary without writing anything for
them, hand over the very brief the CLI path would have sent, and store the
answer through the same rules as a host answer — one summary per session, an
honest ``platform_source``, the call counted.

No test here calls a model. The pins are about what the store ends up holding.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cartograph import hook
from cartograph.mem import ingest, store, summarise, sync


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
    """The machine this decision is for: VS Code, and no `copilot` binary."""
    monkeypatch.setattr(summarise.shutil, "which", lambda binary: None)


def _capture(repo: Path, session: str, *texts: str) -> None:
    for text in texts:
        assert ingest.capture(repo, {"session_id": session, "prompt": text}, host="copilot-chat")


def _line(kind: str, data: dict, timestamp: str = "2026-09-29T14:14:23.964Z") -> str:
    return json.dumps({"type": kind, "data": data, "id": f"{kind}-{len(json.dumps(data))}",
                       "timestamp": timestamp, "parentId": None})


def _chat_log(user_dir: Path, folder: Path, session: str, turns: list[tuple[str, str]],
              *, quiet: bool = True) -> Path:
    """A Copilot Chat transcript with a reply to every prompt, settled unless told not."""
    ws = user_dir / "workspaceStorage" / "ws1"
    transcripts = ws / "GitHub.copilot-chat" / "transcripts"
    transcripts.mkdir(parents=True, exist_ok=True)
    (ws / "workspace.json").write_text(
        json.dumps({"folder": folder.resolve().as_uri()}), encoding="utf-8")
    # Settling is judged by the messages' own timestamps; a live chat's are now.
    at = "2026-09-29T14:14:23.964Z" if quiet else time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    lines = [_line("session.start", {"sessionId": session, "version": 1,
                                     "producer": "copilot-agent"}, at)]
    for n, (prompt, reply) in enumerate(turns):
        lines += [
            _line("user.message", {"content": prompt}, at),
            _line("assistant.turn_start", {"turnId": str(n)}, at),
            _line("assistant.message", {"messageId": f"m{n}", "content": reply}, at),
            _line("assistant.turn_end", {"turnId": str(n)}, at),
        ]
    path = transcripts / f"{session}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if quiet:
        old = time.time() - sync.SETTLE_SECONDS - 60
        os.utime(path, (old, old))
    return path


def _sessions(repo: Path) -> list[dict]:
    with store.MemoryStore(store.db_path(repo, create=False)) as memory:
        return [dict(row) for row in memory._conn.execute(  # noqa: SLF001
            "SELECT * FROM observations WHERE doc_type = 'sessions' ORDER BY created_at")]


def _meta(repo: Path, key: str) -> str | None:
    with store.MemoryStore(store.db_path(repo, create=False)) as memory:
        return memory.get_meta(key)


_TURNS = [("Should the cursor use keyset paging", "Keyset breaks: the sort key is not unique."),
          ("Then use offset paging, decided", "Switched to offset paging.")]

_ANSWER = ("TITLE: Cursor paging moved to offset\nWORKED ON: paging.\n"
           "DECIDED: offset paging.\nPROPOSED: none\nDEAD ENDS: keyset, key not unique.")


# --- the brief, without writing ----------------------------------------------


def test_brief_only_is_the_brief_the_cli_would_send_replies_included(repo, tmp_path, no_cli):
    user = tmp_path / "User"
    _chat_log(user, repo, "s1", _TURNS)
    sync.sync(repo, user_dirs=[user], copilot_dir=tmp_path / "copilot")

    result = summarise.summarise(repo, session="s1", brief_only=True, user_dirs=[user])

    with store.MemoryStore(store.db_path(repo, create=False)) as memory:
        prompts = memory.session_documents("s1", doc_type="prompts",
                                           limit=summarise.MAX_PROMPTS)
    assert result["brief"] == summarise.brief(prompts, [reply for _, reply in _TURNS])
    assert "Keyset breaks: the sort key is not unique." in result["brief"]
    assert result["prompts"] == 2
    assert result["observation"] is None


def test_brief_only_writes_nothing_and_spends_nothing(repo, tmp_path, no_cli):
    _capture(repo, "s1", "First prompt of the session", "Second prompt of the session")

    summarise.summarise(repo, session="s1", brief_only=True, user_dirs=[tmp_path / "User"])

    assert _sessions(repo) == []
    assert _meta(repo, summarise.COST_HOST_CALLS) is None
    assert _meta(repo, summarise.COST_RAW_CHARS + "s1") is None


def test_brief_only_says_when_there_is_nothing_to_brief(repo, tmp_path, no_cli):
    _capture(repo, "s1", "First prompt of the session", "Second prompt of the session")
    summarise.summarise(repo, session="s1", use_host=False)

    result = summarise.summarise(repo, session="s1", brief_only=True,
                                 user_dirs=[tmp_path / "User"])

    assert result["already_summarised"] is True
    assert "brief" not in result


# --- storing an answer obtained elsewhere ----------------------------------


def test_an_answer_is_stored_as_host_agent_under_the_callers_label(repo, tmp_path, no_cli):
    _capture(repo, "s1", "First prompt of the session", "Second prompt of the session")

    result = summarise.summarise(repo, session="s1", answer=_ANSWER,
                                 summarised_by="vscode-lm:auto",
                                 user_dirs=[tmp_path / "User"])

    [row] = _sessions(repo)
    assert row["summary_source"] == "host-agent"
    assert row["platform_source"] == "vscode-lm:auto"
    assert row["title"] == "Cursor paging moved to offset"
    assert "TITLE:" not in row["body"] and "DEAD ENDS: keyset" in row["body"]
    assert result["summary_source"] == "host-agent"
    assert result["host"] == "vscode-lm:auto"


def test_a_stored_answer_counts_as_one_summary_call(repo, tmp_path, no_cli):
    _capture(repo, "s1", "First prompt of the session", "Second prompt of the session")

    summarise.summarise(repo, session="s1", answer=_ANSWER, summarised_by="vscode-lm:auto",
                        user_dirs=[tmp_path / "User"])

    assert _meta(repo, summarise.COST_HOST_CALLS) == "1"
    assert _meta(repo, summarise.COST_RAW_CHARS + "s1")


def test_an_answer_never_makes_a_second_summary(repo, tmp_path, no_cli):
    _capture(repo, "s1", "First prompt of the session", "Second prompt of the session")
    summarise.summarise(repo, session="s1", use_host=False)

    result = summarise.summarise(repo, session="s1", answer=_ANSWER,
                                 summarised_by="vscode-lm:auto",
                                 user_dirs=[tmp_path / "User"])

    assert result["already_summarised"] is True
    assert [row["summary_source"] for row in _sessions(repo)] == ["structural"]
    assert _meta(repo, summarise.COST_HOST_CALLS) == "0"


def test_an_empty_answer_is_not_a_summary(repo, tmp_path, no_cli):
    _capture(repo, "s1", "First prompt of the session", "Second prompt of the session")

    result = summarise.summarise(repo, session="s1", answer="  \n", summarised_by="vscode-lm:auto",
                                 user_dirs=[tmp_path / "User"])

    [row] = _sessions(repo)
    assert row["summary_source"] == "structural"
    assert row["platform_source"] is None
    assert result["fallback_reason"]
    # It still spent a request on the person's quota.
    assert _meta(repo, summarise.COST_HOST_CALLS) == "1"


def test_an_answer_needs_a_label_saying_who_wrote_it(repo, no_cli):
    _capture(repo, "s1", "First prompt of the session", "Second prompt of the session")
    with pytest.raises(ValueError):
        summarise.summarise(repo, session="s1", answer=_ANSWER, summarised_by="copilot-cli")


# --- handing off from sync --------------------------------------------------


def test_hand_off_writes_nothing_and_names_the_sessions_awaiting(repo, tmp_path, no_cli):
    user = tmp_path / "User"
    _chat_log(user, repo, "done", _TURNS)

    result = sync.sync(repo, user_dirs=[user], copilot_dir=tmp_path / "copilot",
                       summarise_sessions=True, hand_off="no-cli")

    assert result["awaiting_summary"] == [{"session": "done", "prompts": 2}]
    assert result["summarised"] == []
    assert _sessions(repo) == []
    assert _meta(repo, summarise.COST_HOST_CALLS) is None


def test_hand_off_leaves_a_session_still_being_written(repo, tmp_path, no_cli):
    user = tmp_path / "User"
    _chat_log(user, repo, "live", _TURNS, quiet=False)

    result = sync.sync(repo, user_dirs=[user], copilot_dir=tmp_path / "copilot",
                       summarise_sessions=True, hand_off="no-cli")

    assert result["awaiting_summary"] == []
    assert [w["session"] for w in result["waiting"]] == ["live"]


def test_without_hand_off_no_cli_still_means_structural(repo, tmp_path, no_cli):
    user = tmp_path / "User"
    _chat_log(user, repo, "done", _TURNS)

    result = sync.sync(repo, user_dirs=[user], copilot_dir=tmp_path / "copilot",
                       summarise_sessions=True)

    assert "awaiting_summary" not in result
    assert [row["summary_source"] for row in _sessions(repo)] == ["structural"]


def test_hand_off_is_not_taken_when_the_cli_can_summarise(repo, tmp_path, monkeypatch):
    user = tmp_path / "User"
    _chat_log(user, repo, "done", _TURNS)
    monkeypatch.setattr(summarise, "available_hosts", lambda: [summarise.HOSTS[0]])
    monkeypatch.setattr(summarise, "call_host", lambda host, text: _ANSWER)

    result = sync.sync(repo, user_dirs=[user], copilot_dir=tmp_path / "copilot",
                       summarise_sessions=True, hand_off="no-cli")

    assert "awaiting_summary" not in result
    assert [row["platform_source"] for row in _sessions(repo)] == ["copilot-cli"]


def test_hand_off_always_skips_the_cli(repo, tmp_path, monkeypatch):
    user = tmp_path / "User"
    _chat_log(user, repo, "done", _TURNS)
    called: list[str] = []
    monkeypatch.setattr(summarise, "available_hosts", lambda: [summarise.HOSTS[0]])
    monkeypatch.setattr(summarise, "call_host", lambda host, text: called.append(text))

    result = sync.sync(repo, user_dirs=[user], copilot_dir=tmp_path / "copilot",
                       summarise_sessions=True, hand_off="always")

    assert [a["session"] for a in result["awaiting_summary"]] == ["done"]
    assert called == []
    assert _sessions(repo) == []


def test_the_depth_cap_still_wins_over_hand_off(repo, tmp_path, no_cli, monkeypatch):
    user = tmp_path / "User"
    _chat_log(user, repo, "done", _TURNS)
    monkeypatch.setenv(summarise.SUMMARISE_MARKER, "1")

    result = sync.sync(repo, user_dirs=[user], copilot_dir=tmp_path / "copilot",
                       summarise_sessions=True, hand_off="always")

    assert "awaiting_summary" not in result
    assert [row["summary_source"] for row in _sessions(repo)] == ["structural"]


# --- the CLI surface --------------------------------------------------------


def _run_cli(*args: str) -> tuple[int, dict]:
    engine = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "-m", "cartograph", *args, "--format", "json"],
        capture_output=True, text=True, timeout=120, cwd=engine, stdin=subprocess.DEVNULL,
        # No `copilot` on PATH: this is the machine the hand-off exists for.
        env={**os.environ, "PYTHONPATH": str(engine), "PATH": os.path.dirname(sys.executable)},
    )
    return proc.returncode, json.loads(proc.stdout)


def test_cli_brief_then_answer_then_status(repo, tmp_path):
    _capture(repo, "s1", "First prompt of the session", "Second prompt of the session")
    empty_user = str(tmp_path / "User")

    code, brief = _run_cli("mem", "summarise", "--repo", str(repo), "--session", "s1",
                           "--brief-only", "--vscode-user-dir", empty_user)
    assert code == 0
    assert brief["data"]["brief"].startswith("Summarise one coding session")
    assert brief["data"]["prompts"] == 2

    answer = tmp_path / "answer.txt"
    answer.write_text(_ANSWER, encoding="utf-8")
    code, stored = _run_cli("mem", "summarise", "--repo", str(repo), "--session", "s1",
                            "--answer-file", str(answer), "--summarised-by", "vscode-lm:auto",
                            "--vscode-user-dir", empty_user)
    assert code == 0
    assert stored["data"]["observation"]["platform_source"] == "vscode-lm:auto"
    assert stored["data"]["summary_source"] == "host-agent"

    code, status = _run_cli("mem", "status", "--repo", str(repo))
    assert status["data"]["latest_summary_by"] == "vscode-lm:auto"


def test_cli_status_says_structural_when_no_model_wrote_it(repo):
    _capture(repo, "s1", "First prompt of the session", "Second prompt of the session")
    _run_cli("mem", "summarise", "--repo", str(repo), "--session", "s1", "--no-host-agent")

    code, status = _run_cli("mem", "status", "--repo", str(repo))

    assert status["data"]["latest_summary_by"] == "structural"


@pytest.mark.parametrize("extra", [
    ["--brief-only", "--no-host-agent"],
    ["--answer-file", "x.txt"],
    ["--summarised-by", "vscode-lm:auto"],
    ["--answer-file", "x.txt", "--summarised-by", "copilot-cli"],
    ["--brief-only", "--pending"],
])
def test_cli_refuses_contradictory_flags_as_usage(repo, extra):
    _capture(repo, "s1", "First prompt of the session", "Second prompt of the session")

    code, doc = _run_cli("mem", "summarise", "--repo", str(repo), "--session", "s1", *extra)

    assert code == 1
    assert doc["ok"] is False
    assert _sessions(repo) == []


def test_cli_sync_hands_off(repo, tmp_path):
    user = tmp_path / "User"
    _chat_log(user, repo, "done", _TURNS)

    code, doc = _run_cli("mem", "sync", "--repo", str(repo), "--summarise",
                         "--hand-off", "no-cli", "--vscode-user-dir", str(user))

    assert code == 0
    assert doc["data"]["awaiting_summary"] == [{"session": "done", "prompts": 2}]
    assert _sessions(repo) == []


def test_the_catalogue_offers_the_new_flags():
    code, doc = _run_cli("capabilities", "--command", "mem summarise")
    flags = {flag["flag"] for flag in doc["data"]["commands"][0]["flags"]}
    assert {"--brief-only", "--answer-file", "--summarised-by"} <= flags

    code, doc = _run_cli("capabilities", "--command", "mem sync")
    flags = {flag["flag"]: flag for flag in doc["data"]["commands"][0]["flags"]}
    assert flags["--hand-off"]["choices"] == ["no-cli", "always"]


# --- the Chat catch-up hook -------------------------------------------------
#
# VS Code has no SessionEnd, so a Chat session is summarised by the next
# session's SessionStart hook. On a machine with no `copilot`, that hook would
# write a structural summary minutes before the extension could offer VS
# Code's models — spending the session's one summary on the fallback.


def _catchup(repo: Path, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    _capture(repo, "old", "Question one in the old session", "Question two in the old one")
    spawned: list[list[str]] = []
    monkeypatch.setattr(hook, "spawn_detached", lambda argv, **kw: spawned.append(argv) or True)
    monkeypatch.setattr(hook, "hook_payload", lambda stream=None: {"session_id": "new"})
    return spawned


def test_the_chat_catchup_hands_off(repo, monkeypatch):
    spawned = _catchup(repo, monkeypatch)

    hook.run("session-catchup", repo=str(repo), host="copilot")

    [argv] = spawned
    assert argv[argv.index("--hand-off") + 1] == "no-cli"


def test_the_cli_catchup_does_not_hand_off(repo, monkeypatch):
    """Copilot CLI fired it, so `copilot` is there to write the summary."""
    monkeypatch.setenv("COPILOT_CLI", "1")
    spawned = _catchup(repo, monkeypatch)

    hook.run("session-catchup", repo=str(repo), host="copilot")

    [argv] = spawned
    assert "--hand-off" not in argv


def test_pending_with_hand_off_writes_nothing_without_a_cli(repo):
    _capture(repo, "old", "Question one in the old session", "Question two in the old one")

    code, doc = _run_cli("mem", "summarise", "--repo", str(repo), "--pending",
                         "--exclude-session", "new", "--hand-off", "no-cli")

    assert code == 0
    assert doc["data"]["awaiting_summary"] == [{"session": "old", "prompts": 2}]
    assert doc["data"]["sessions"] == []
    assert _sessions(repo) == []


def test_hand_off_on_summarise_needs_pending(repo):
    _capture(repo, "s1", "First prompt of the session", "Second prompt of the session")

    code, _ = _run_cli("mem", "summarise", "--repo", str(repo), "--session", "s1",
                       "--hand-off", "no-cli")

    assert code == 1


# --- why a summary is structural, kept in the store ------------------------
#
# The extension decides in a window why it could not use a model, and that
# window's memory is gone at reload. `mem status` must still say why the latest
# summary is structural, so the reason is persisted with the session it
# explains.


def test_a_callers_fallback_reason_is_stored_and_reported(repo, no_cli):
    _capture(repo, "s1", "First prompt of the session", "Second prompt of the session")

    result = summarise.summarise(repo, session="s1", use_host=False,
                                 fallback_reason='model "auto" not offered')

    assert result["fallback_reason"] == 'model "auto" not offered'
    with store.MemoryStore(store.db_path(repo, create=False)) as memory:
        assert summarise.fallback_reason_for(memory, "s1") == 'model "auto" not offered'


def test_status_names_the_reason_beside_structural(repo):
    _capture(repo, "s1", "First prompt of the session", "Second prompt of the session")
    _run_cli("mem", "summarise", "--repo", str(repo), "--session", "s1", "--no-host-agent",
             "--fallback-reason", 'model "auto" not offered')

    code, status = _run_cli("mem", "status", "--repo", str(repo))

    assert code == 0
    assert status["data"]["latest_summary_by"] == 'structural (model "auto" not offered)'


def test_the_engines_own_fallback_reason_is_kept_too(repo, no_cli):
    _capture(repo, "s1", "First prompt of the session", "Second prompt of the session")

    summarise.summarise(repo, session="s1")

    with store.MemoryStore(store.db_path(repo, create=False)) as memory:
        assert "no host agent CLI on PATH" in (summarise.fallback_reason_for(memory, "s1") or "")


def test_structural_by_choice_has_no_reason_to_report(repo, no_cli):
    # --no-host-agent alone is somebody's choice, not a failure to explain.
    _capture(repo, "s1", "First prompt of the session", "Second prompt of the session")

    summarise.summarise(repo, session="s1", use_host=False)

    with store.MemoryStore(store.db_path(repo, create=False)) as memory:
        assert summarise.fallback_reason_for(memory, "s1") is None


def test_a_later_model_summary_does_not_inherit_an_older_reason(repo, tmp_path, no_cli):
    _capture(repo, "s1", "First prompt of the session", "Second prompt of the session")
    _capture(repo, "s2", "Third prompt, other session", "Fourth prompt, other session")
    summarise.summarise(repo, session="s1", use_host=False, fallback_reason="consent refused")
    time.sleep(0.01)
    summarise.summarise(repo, session="s2", answer=_ANSWER, summarised_by="vscode-lm:auto",
                        user_dirs=[tmp_path / "User"])

    code, status = _run_cli("mem", "status", "--repo", str(repo))

    assert status["data"]["latest_summary_by"] == "vscode-lm:auto"


def test_the_reason_is_one_short_line(repo, no_cli):
    _capture(repo, "s1", "First prompt of the session", "Second prompt of the session")

    summarise.summarise(repo, session="s1", use_host=False,
                        fallback_reason="first line\nsecond line " + "x" * 500)

    with store.MemoryStore(store.db_path(repo, create=False)) as memory:
        reason = summarise.fallback_reason_for(memory, "s1") or ""
    assert "\n" not in reason
    assert reason.startswith("first line second line")
    assert len(reason) <= summarise.MAX_REASON_CHARS


def test_sync_passes_the_reason_to_every_structural_summary(repo, tmp_path, no_cli):
    user = tmp_path / "User"
    _chat_log(user, repo, "done", _TURNS)

    sync.sync(repo, user_dirs=[user], copilot_dir=tmp_path / "copilot",
              summarise_sessions=True, use_host=False, fallback_reason="consent refused")

    with store.MemoryStore(store.db_path(repo, create=False)) as memory:
        assert summarise.fallback_reason_for(memory, "done") == "consent refused"


def test_cli_sync_takes_the_reason(repo, tmp_path):
    user = tmp_path / "User"
    _chat_log(user, repo, "done", _TURNS)

    code, _ = _run_cli("mem", "sync", "--repo", str(repo), "--vscode-user-dir", str(user),
                       "--summarise", "--no-host-agent", "--fallback-reason", "consent refused")
    code, status = _run_cli("mem", "status", "--repo", str(repo))

    assert status["data"]["latest_summary_by"] == "structural (consent refused)"


@pytest.mark.parametrize("command", [
    ["summarise", "--session", "s1"],
    ["summarise", "--session", "s1", "--brief-only"],
    ["sync", "--summarise"],
    ["sync"],
])
def test_a_reason_without_no_host_agent_is_a_usage_error(repo, command):
    _capture(repo, "s1", "First prompt of the session", "Second prompt of the session")

    code, doc = _run_cli("mem", *command, "--repo", str(repo), "--fallback-reason", "why")

    assert code == 1
    assert doc["ok"] is False
    assert _sessions(repo) == []
