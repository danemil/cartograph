"""``carto mem sync`` — memory from the logs Copilot writes itself.

Hooks are the primary capture path, and an organisation can switch them off:
on the first Linux test machine ``chat.useHooks`` was disabled by policy and
Chat captured nothing. Copilot writes its own conversation logs regardless, so
sync imports prompts from them. The fixtures below copy the shapes read off
real files on 2026-09-29 (VS Code 1.139.1 / Copilot Chat 0.67.0, and Copilot
CLI 1.0.82) — see ``docs/copilot-hooks.md``.

The pins that matter most: a prompt a hook already recorded is not recorded
again, sessions from other folders are never imported, and the import says
plainly whether hooks were firing — a silent fallback is how the Linux
failure went unnoticed.
"""

from __future__ import annotations

import json
import os
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


def _line(kind: str, data: dict, timestamp: str = "2026-09-29T14:14:23.964Z") -> str:
    return json.dumps({
        "type": kind, "data": data, "id": f"id-{kind}-{len(json.dumps(data))}",
        "timestamp": timestamp, "parentId": None,
    })


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _chat_log(user_dir: Path, folder: Path, session: str, prompts: list[str],
              *, workspace: str = "ws1", folder_uri: str | None = None,
              timestamp: str = "2026-09-29T14:14:23.964Z") -> Path:
    """A VS Code workspaceStorage entry with one Copilot Chat transcript."""
    ws = user_dir / "workspaceStorage" / workspace
    transcripts = ws / "GitHub.copilot-chat" / "transcripts"
    transcripts.mkdir(parents=True, exist_ok=True)
    (ws / "workspace.json").write_text(
        json.dumps({"folder": folder_uri or folder.resolve().as_uri()}), encoding="utf-8"
    )
    lines = [_line("session.start", {
        "sessionId": session, "version": 1, "producer": "copilot-agent",
        "copilotVersion": "0.67.0", "vscodeVersion": "1.139.1",
    }, timestamp)]
    for n, prompt in enumerate(prompts):
        lines += [
            _line("user.message", {"content": prompt}, timestamp),
            _line("assistant.turn_start", {"turnId": str(n)}, timestamp),
            _line("assistant.message", {"messageId": f"m{n}", "content": f"reply {n}"}, timestamp),
            _line("assistant.turn_end", {"turnId": str(n)}, timestamp),
        ]
    path = transcripts / f"{session}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _cli_log(copilot_dir: Path, git_root: Path, session: str, prompts: list[str],
             *, hooks: bool) -> Path:
    """A Copilot CLI session-state entry, with or without hook events in it."""
    state = copilot_dir / "session-state" / session
    state.mkdir(parents=True, exist_ok=True)
    lines = [_line("session.start", {
        "sessionId": session, "version": 1, "producer": "copilot-agent",
        "copilotVersion": "1.0.82",
        "context": {"cwd": str(git_root), "gitRoot": str(git_root), "branch": "master"},
    })]
    for prompt in prompts:
        if hooks:
            lines.append(_line("hook.start", {
                "hookInvocationId": "h", "hookType": "userPromptSubmitted",
                "input": {"sessionId": session, "prompt": prompt},
            }))
        lines += [
            _line("user.message", {"content": prompt}),
            _line("assistant.message", {"content": "ok"}),
        ]
    path = state / "events.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _rows(repo: Path, doc_type: str = "prompts") -> list[dict]:
    with store.MemoryStore(store.db_path(repo, create=False)) as memory:
        return [
            dict(row)
            for row in memory._conn.execute(  # noqa: SLF001 — asserting storage
                "SELECT * FROM observations WHERE doc_type = ? ORDER BY created_at, id",
                (doc_type,),
            ).fetchall()
        ]


def _sync(repo: Path, tmp_path: Path, **kw):
    return sync.sync(
        repo, user_dirs=[tmp_path / "User"], copilot_dir=tmp_path / "copilot", **kw
    )


# --- Copilot Chat ------------------------------------------------------------


def test_chat_prompts_are_imported_as_copilot_chat(repo, tmp_path):
    _chat_log(tmp_path / "User", repo, "s1", [
        "We need to decide where to keep build caches",
        "We tried /tmp but it was wiped on every reboot",
    ])

    result = _sync(repo, tmp_path)

    rows = _rows(repo)
    assert [r["body"] for r in rows] == [
        "We need to decide where to keep build caches",
        "We tried /tmp but it was wiped on every reboot",
    ]
    assert {r["platform_source"] for r in rows} == {"copilot-chat"}
    assert {r["session"] for r in rows} == {"s1"}
    assert result["hosts"]["copilot-chat"]["imported"] == 2


def test_replies_are_not_recorded_as_prompts(repo, tmp_path):
    _chat_log(tmp_path / "User", repo, "s1", ["A prompt long enough to keep"])
    _sync(repo, tmp_path)
    assert [r["body"] for r in _rows(repo)] == ["A prompt long enough to keep"]


def test_another_folders_chats_are_never_imported(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    _chat_log(tmp_path / "User", other, "s-other", ["A prompt from another repo"],
              workspace="ws2")

    result = _sync(repo, tmp_path)

    assert not store.db_path(repo, create=False).exists()
    assert result["hosts"]["copilot-chat"]["files"] == 0


def test_a_remote_workspace_uri_still_matches(repo, tmp_path):
    """Over Remote SSH, workspace.json names the folder by a vscode-remote URI."""
    uri = f"vscode-remote://ssh-remote%2B192.168.99.111{repo.resolve().as_posix()}"
    _chat_log(tmp_path / "User", repo, "s1", ["Asked over a Remote SSH window"],
              folder_uri=uri)
    _sync(repo, tmp_path)
    assert len(_rows(repo)) == 1


def test_a_half_written_last_line_is_skipped_not_fatal(repo, tmp_path):
    path = _chat_log(tmp_path / "User", repo, "s1", ["The complete first prompt"])
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"type": "user.message", "data": {"content": "half wr')

    _sync(repo, tmp_path)

    assert [r["body"] for r in _rows(repo)] == ["The complete first prompt"]


# --- hooks and logs together -------------------------------------------------


def test_a_prompt_a_hook_already_recorded_is_not_imported_again(repo, tmp_path):
    text = "Recorded by the hook first, then seen in the log"
    assert ingest.capture(repo, {"session_id": "s1", "prompt": text}, host="copilot-chat")
    _chat_log(tmp_path / "User", repo, "s1", [text])

    result = _sync(repo, tmp_path)

    assert len(_rows(repo)) == 1
    chat = result["hosts"]["copilot-chat"]
    assert (chat["already_recorded"], chat["imported"]) == (1, 0)
    assert chat["capture"] == "hooks"


def test_prompts_the_hooks_missed_mean_capture_is_from_logs(repo, tmp_path):
    _chat_log(tmp_path / "User", repo, "s1", ["Hooks never saw this prompt at all"])

    result = _sync(repo, tmp_path)

    assert result["hosts"]["copilot-chat"]["capture"] == "logs"


def test_running_twice_imports_nothing_the_second_time(repo, tmp_path):
    _chat_log(tmp_path / "User", repo, "s1", ["Imported once and only once"])
    _sync(repo, tmp_path)
    second = _sync(repo, tmp_path)

    assert len(_rows(repo)) == 1
    assert second["hosts"]["copilot-chat"]["imported"] == 0


def test_a_file_that_grew_is_read_again(repo, tmp_path):
    path = _chat_log(tmp_path / "User", repo, "s1", ["The first prompt in the chat"])
    _sync(repo, tmp_path)
    _chat_log(tmp_path / "User", repo, "s1",
              ["The first prompt in the chat", "A second prompt, added later"])
    os.utime(path, (time.time() + 5, time.time() + 5))

    _sync(repo, tmp_path)

    assert len(_rows(repo)) == 2


def test_the_last_result_is_what_mem_status_reports(repo, tmp_path):
    _chat_log(tmp_path / "User", repo, "s1", ["Hooks never saw this prompt at all"])
    _sync(repo, tmp_path)

    with store.MemoryStore(store.db_path(repo, create=False)) as memory:
        status = sync.last_status(memory)

    assert status["copilot-chat"]["capture"] == "logs"


# --- Copilot CLI -------------------------------------------------------------


def test_cli_prompts_are_imported_as_copilot_cli(repo, tmp_path):
    _cli_log(tmp_path / "copilot", repo, "c1",
             ["A CLI prompt with hooks switched off"], hooks=False)

    result = _sync(repo, tmp_path)

    assert [r["platform_source"] for r in _rows(repo)] == ["copilot-cli"]
    assert result["hosts"]["copilot-cli"]["hooks_fired"] is False


def test_the_cli_log_says_whether_its_hooks_ran(repo, tmp_path):
    _cli_log(tmp_path / "copilot", repo, "c1",
             ["A CLI prompt with hooks switched on"], hooks=True)
    result = _sync(repo, tmp_path)
    assert result["hosts"]["copilot-cli"]["hooks_fired"] is True


def test_the_summarisers_own_cli_sessions_are_never_imported(repo, tmp_path):
    """The summariser runs `copilot -p` in the temp directory, not the repo."""
    elsewhere = tmp_path / "tmpdir"
    elsewhere.mkdir()
    _cli_log(tmp_path / "copilot", elsewhere, "c-summ",
             ["Summarise one coding session for a project memory"], hooks=False)

    _sync(repo, tmp_path)

    assert not store.db_path(repo, create=False).exists()


# --- summarising after an import ---------------------------------------------


def test_summarise_skips_a_session_whose_log_is_still_changing(repo, tmp_path):
    _chat_log(tmp_path / "User", repo, "live", ["First prompt of a live chat", "Second prompt of it"],
              timestamp=_now())

    result = _sync(repo, tmp_path, summarise_sessions=True, use_host=False)

    assert result["summarised"] == []
    assert _rows(repo, "sessions") == []


def test_summarise_takes_a_session_that_has_gone_quiet(repo, tmp_path):
    path = _chat_log(tmp_path / "User", repo, "quiet",
                     ["First prompt of a finished chat", "Second prompt of it"])
    old = time.time() - sync.SETTLE_SECONDS - 60
    os.utime(path, (old, old))

    result = _sync(repo, tmp_path, summarise_sessions=True, use_host=False)

    assert [r["session"] for r in result["summarised"]] == ["quiet"]
    assert [r["summary_source"] for r in _rows(repo, "sessions")] == ["structural"]


# --- where the logs live -----------------------------------------------------


def test_default_user_dirs_include_the_remote_server(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    remote = tmp_path / ".vscode-server" / "data" / "User"
    remote.mkdir(parents=True)
    assert remote in sync.vscode_user_dirs()


def test_a_missing_store_and_no_logs_is_a_quiet_no_op(repo, tmp_path):
    result = _sync(repo, tmp_path)
    assert result["hosts"]["copilot-chat"]["capture"] == "unknown"
    assert not store.db_path(repo, create=False).exists()


# --- the command -------------------------------------------------------------


def _cli(repo: Path, *args: str) -> tuple[int, dict]:
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "cartograph", "mem", *args, "--repo", str(repo),
         "--format", "json"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
    )
    return proc.returncode, json.loads(proc.stdout)


def test_sync_runs_where_there_is_no_store_yet(repo, tmp_path):
    """Every other mem read exits 2 without a store; sync is what creates one."""
    code, envelope = _cli(repo, "sync", "--vscode-user-dir", str(tmp_path / "User"))
    assert code == 0 and envelope["ok"] and envelope["tool"] == "mem sync"


def test_sync_then_status_names_the_capture_path(repo, tmp_path):
    _chat_log(tmp_path / "User", repo, "s1", ["Only the log ever saw this prompt"])

    code, envelope = _cli(repo, "sync", "--vscode-user-dir", str(tmp_path / "User"))
    assert code == 0
    assert envelope["data"]["hosts"]["copilot-chat"]["imported"] == 1

    code, status = _cli(repo, "status")
    assert status["data"]["capture_copilot_chat"].startswith("logs (1 imported")


def test_sync_is_in_the_catalogue(repo):
    """Against what the binary prints, like every other catalogue pin."""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "cartograph", "capabilities", "--format", "json"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
    )
    entries = {e["name"]: e for e in json.loads(proc.stdout)["data"]["commands"]}
    assert entries["mem sync"]["when"]
    assert entries["mem sync"]["example"].startswith("carto mem sync")


# --- VS Code's own chat store ------------------------------------------------
#
# Measured on 2026-09-29 with chat.useHooks off (VS Code 1.139.1): Copilot's
# transcript held only `session.start` — no prompt, no reply — while VS Code's
# `chatSessions/<id>.jsonl` held both. It is a snapshot line (kind 0) followed
# by patches: kind 1 sets the value at path `k`, kind 2 appends `v` to the list
# at `k`.


def _chat_session(user_dir: Path, folder: Path, session: str, first: list[str],
                  appended: list[str] = (), *, workspace: str = "ws1",
                  hidden: bool = False) -> Path:
    ws = user_dir / "workspaceStorage" / workspace
    (ws / "chatSessions").mkdir(parents=True, exist_ok=True)
    (ws / "workspace.json").write_text(
        json.dumps({"folder": folder.resolve().as_uri()}), encoding="utf-8"
    )

    def request(text: str) -> dict:
        return {"requestId": f"request_{abs(hash(text))}", "timestamp": 1790695675246,
                "message": {"text": text, "parts": []}, "response": [],
                "hiddenFromTranscript": hidden}

    lines = [{"kind": 0, "v": {"version": 3, "sessionId": session,
                               "customTitle": "t", "requests": [request(t) for t in first]}}]
    lines.append({"kind": 1, "k": ["customTitle"], "v": "A later title"})
    for text in appended:
        lines.append({"kind": 2, "k": ["requests"], "v": [request(text)]})
        lines.append({"kind": 2, "k": ["requests", len(first), "response"],
                      "v": [{"value": "a reply"}]})
    path = ws / "chatSessions" / f"{session}.jsonl"
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
    return path


def test_chat_sessions_supply_the_prompts_the_transcript_lacks(repo, tmp_path):
    """The exact state measured with hooks off: an empty transcript."""
    user = tmp_path / "User"
    _chat_log(user, repo, "s1", [])  # session.start only
    _chat_session(user, repo, "s1", ["How should io.load handle missing files?"])

    result = _sync(repo, tmp_path)

    assert [(r["session"], r["body"]) for r in _rows(repo)] == [
        ("s1", "How should io.load handle missing files?")
    ]
    assert result["hosts"]["copilot-chat"]["capture"] == "logs"


def test_appended_requests_are_replayed(repo, tmp_path):
    _chat_session(tmp_path / "User", repo, "s1",
                  ["The first prompt of this chat"], ["The second prompt, appended later"])
    _sync(repo, tmp_path)
    assert [r["body"] for r in _rows(repo)] == [
        "The first prompt of this chat", "The second prompt, appended later",
    ]


def test_both_stores_holding_one_prompt_record_it_once(repo, tmp_path):
    user = tmp_path / "User"
    _chat_log(user, repo, "s1", ["Held by the transcript and the chat store"])
    _chat_session(user, repo, "s1", ["Held by the transcript and the chat store"])
    _sync(repo, tmp_path)
    assert len(_rows(repo)) == 1


def test_hidden_requests_are_not_prompts(repo, tmp_path):
    _chat_session(tmp_path / "User", repo, "s1", ["Something VS Code sent itself"],
                  hidden=True)
    _sync(repo, tmp_path)
    assert not store.db_path(repo, create=False).exists()


# --- Remote windows: the mirror ----------------------------------------------
#
# Measured on 2026-09-29, Windows host → Remote SSH → Ubuntu VM: VS Code keeps
# chatSessions on the Windows side, and the VM has none. The companion
# extension, running on the Windows side, copies each chat file into
# `.cartograph/chatSessions/` in the repository on the remote, and the same
# reader handles it there.


def test_mirrored_chat_sessions_are_imported(repo, tmp_path):
    mirror_parent = tmp_path / "unused-user"
    path = _chat_session(mirror_parent, repo, "s-remote", ["Asked in a Remote SSH window"])
    mirror = repo / ".cartograph" / "chatSessions"
    mirror.mkdir(parents=True)
    (mirror / path.name).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    result = _sync(repo, tmp_path)  # no VS Code user dir holds this chat

    assert [(r["session"], r["platform_source"]) for r in _rows(repo)] == [
        ("s-remote", "copilot-chat")
    ]
    assert result["hosts"]["copilot-chat"]["capture"] == "logs"


# --- found on the Remote SSH VM, 2026-09-29 ------------------------------------


def test_prompts_imported_earlier_still_count_as_logs(repo, tmp_path):
    """The VM reported `hooks` two minutes after the import that recorded both
    prompts: the file changed, the next pass found them already recorded, and
    already-recorded was taken to mean a hook had done it."""
    path = _chat_session(tmp_path / "User", repo, "s1", ["Imported from the log, never by a hook"])
    _sync(repo, tmp_path)
    os.utime(path, (time.time() + 5, time.time() + 5))

    second = _sync(repo, tmp_path)

    chat = second["hosts"]["copilot-chat"]
    assert chat["imported"] == 0
    assert chat["capture"] == "logs"


def test_a_workspace_dir_is_read_without_a_workspace_json(repo, tmp_path):
    """The remote's workspaceStorage/<id>/ has Copilot's transcripts but no
    workspace.json to match on; the extension names the directory instead."""
    ws = tmp_path / "remote-ws"
    (ws / "GitHub.copilot-chat" / "transcripts").mkdir(parents=True)
    (ws / "GitHub.copilot-chat" / "transcripts" / "s1.jsonl").write_text(
        "\n".join([
            _line("session.start", {"sessionId": "s1"}),
            _line("user.message", {"content": "In the remote transcript only"}),
        ]) + "\n",
        encoding="utf-8",
    )

    sync.sync(repo, user_dirs=[], copilot_dir=tmp_path / "copilot", workspace_dirs=[ws])

    assert [r["body"] for r in _rows(repo)] == ["In the remote transcript only"]
