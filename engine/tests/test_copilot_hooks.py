"""Copilot CLI and Copilot Chat: one hook file, two hosts, no double capture.

Everything pinned here was read off real hook payloads on 2026-09-29, from
Copilot CLI 1.0.82 and VS Code 1.139.1 — see ``docs/copilot-hooks.md`` for the
probe and what it showed. The three facts the design rests on:

- Both hosts load ``.github/hooks/*.json``, and the CLI runs a file in *either*
  schema. Two files would capture every CLI prompt twice, so there is one, in
  the VS Code schema, which both accept.
- The payloads are identical in shape, so the payload cannot say which host
  sent it. The CLI sets ``COPILOT_CLI=1`` in the hook's environment and VS Code
  does not; that is a fact the host states, not a guess from field names.
- VS Code has no ``SessionEnd``. A session is therefore also summarised the
  next time one *starts*, which covers Chat and any session that crashed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cartograph import hook, skills
from cartograph.mem import ingest, store, summarise


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (hook.REENTRY_MARKER, summarise.SUMMARISE_MARKER, "COPILOT_CLI"):
        monkeypatch.delenv(name, raising=False)


def _rows(repo: Path, doc_type: str) -> list[dict]:
    with store.MemoryStore(store.db_path(repo, create=False)) as memory:
        return [
            dict(row)
            for row in memory._conn.execute(  # noqa: SLF001 — asserting storage
                "SELECT * FROM observations WHERE doc_type = ? ORDER BY created_at, id",
                (doc_type,),
            ).fetchall()
        ]


def _vscode_payload(prompt: str, session: str = "s-chat") -> dict:
    """The shape both hosts send for the VS Code schema, as captured."""
    return {
        "timestamp": "2026-09-29T14:14:23.964Z",
        "hook_event_name": "UserPromptSubmit",
        "session_id": session,
        "transcript_path": "/tmp/transcripts/x.jsonl",
        "prompt": prompt,
        "cwd": "/repo",
    }


def _capture(repo: Path, session: str, *texts: str) -> None:
    for text in texts:
        assert ingest.capture(repo, _vscode_payload(text, session), host="copilot-chat")


# --- which host --------------------------------------------------------------


def test_the_cli_is_named_by_the_environment_it_sets(monkeypatch):
    monkeypatch.setenv("COPILOT_CLI", "1")
    assert hook.resolve_host("copilot") == "copilot-cli"


def test_without_the_cli_marker_it_is_chat(monkeypatch):
    assert hook.resolve_host("copilot") == "copilot-chat"


def test_an_explicit_host_is_left_alone(monkeypatch):
    monkeypatch.setenv("COPILOT_CLI", "1")
    assert hook.resolve_host("copilot-chat") == "copilot-chat"
    assert hook.resolve_host(None) is None


def test_capture_records_the_resolved_host(repo, monkeypatch):
    monkeypatch.setenv("COPILOT_CLI", "1")
    monkeypatch.setattr(
        hook, "hook_payload",
        lambda stream=None: _vscode_payload("Explain the review-context shape please"),
    )
    assert hook.run("prompt-capture", repo=str(repo), host="copilot") == 0
    assert [row["platform_source"] for row in _rows(repo, "prompts")] == ["copilot-cli"]


# --- no double capture -------------------------------------------------------


def test_the_same_prompt_in_the_same_session_is_recorded_once(repo):
    """Two hook files, or a Claude-format file VS Code was told to read as well,
    fire the same event twice with the same payload."""
    payload = _vscode_payload("Why does the build take so long here?")
    assert ingest.capture(repo, payload, host="copilot-chat")
    assert not ingest.capture(repo, payload, host="copilot-chat")
    assert len(_rows(repo, "prompts")) == 1


def test_the_same_prompt_in_another_session_is_a_new_row(repo):
    text = "Why does the build take so long here?"
    assert ingest.capture(repo, _vscode_payload(text, "a"), host="copilot-chat")
    assert ingest.capture(repo, _vscode_payload(text, "b"), host="copilot-chat")
    assert len(_rows(repo, "prompts")) == 2


# --- summarising what did not end cleanly ------------------------------------


def test_pending_summarises_every_unsummarised_session_but_the_current(repo):
    _capture(repo, "old-1", "First question about parsing", "Second question about parsing")
    _capture(repo, "old-2", "A question about hooks here", "Another one about hooks here")
    _capture(repo, "now", "The session that is just starting", "and is still going on")

    results = summarise.summarise_pending(repo, exclude="now", use_host=False)

    summarised = {row["session"] for row in _rows(repo, "sessions")}
    assert summarised == {"old-1", "old-2"}
    assert all(r["observation"] for r in results)


def test_pending_skips_sessions_already_summarised_or_too_short(repo):
    _capture(repo, "done", "Question one for the done session", "Question two for it")
    summarise.summarise(repo, session="done", use_host=False)
    _capture(repo, "short", "Only one prompt in this session")

    assert summarise.summarise_pending(repo, use_host=False) == []
    assert len(_rows(repo, "sessions")) == 1


def test_pending_is_bounded_per_run(repo):
    for n in range(summarise.MAX_PENDING + 2):
        _capture(repo, f"s{n}", f"Question one in session {n}", f"Question two in session {n}")

    results = summarise.summarise_pending(repo, use_host=False)

    assert len(results) == summarise.MAX_PENDING


def test_session_catchup_spawns_and_names_the_session_to_leave_alone(repo, monkeypatch):
    _capture(repo, "old", "Question one in the old session", "Question two in the old one")
    spawned: list[list[str]] = []
    monkeypatch.setattr(hook, "spawn_detached", lambda argv, **kw: spawned.append(argv) or True)
    monkeypatch.setattr(
        hook, "hook_payload",
        lambda stream=None: {"hook_event_name": "SessionStart", "session_id": "new"},
    )

    assert hook.run("session-catchup", repo=str(repo)) == 0

    assert spawned == [hook.catchup_argv(repo, "new")]
    argv = spawned[0]
    assert "--pending" in argv
    assert argv[argv.index("--exclude-session") + 1] == "new"


def test_session_catchup_prints_nothing(repo, monkeypatch, capsys):
    """VS Code parses a hook's stdout as JSON; a status line there is an error."""
    _capture(repo, "old", "Question one in the old session", "Question two in the old one")
    monkeypatch.setattr(hook, "spawn_detached", lambda argv, **kw: True)
    monkeypatch.setattr(hook, "hook_payload", lambda stream=None: {"session_id": "new"})

    hook.run("session-catchup", repo=str(repo))

    assert capsys.readouterr().out == ""


def test_session_catchup_without_a_store_spawns_nothing(repo, monkeypatch):
    monkeypatch.setattr(
        hook, "spawn_detached", lambda *a, **k: pytest.fail("spawned with no store")
    )
    monkeypatch.setattr(hook, "hook_payload", lambda stream=None: {"session_id": "new"})
    assert hook.run("session-catchup", repo=str(repo)) == 0


def test_the_summariser_never_runs_the_host_inside_the_checkout(monkeypatch, repo):
    """The detached child's cwd IS the repository, and a trusted checkout's
    ``.github/hooks`` would load into the host session the summariser starts."""
    seen: dict = {}

    def record(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "TITLE: t", "")

    monkeypatch.chdir(repo)
    monkeypatch.setattr(summarise.subprocess, "run", record)
    summarise.call_host(summarise.HOSTS[0], "brief")

    assert Path(seen["cwd"]).resolve() != repo.resolve()


# --- the hook file -------------------------------------------------------------


def _commands(config: dict, event: str) -> list[dict]:
    return config["hooks"][event]


def test_the_file_wires_the_four_moments():
    config = skills.generate_copilot_hooks_config()
    jobs = {
        event: [entry["command"] for entry in entries]
        for event, entries in config["hooks"].items()
    }
    assert set(jobs) == {"SessionStart", "UserPromptSubmit", "Stop", "SessionEnd"}
    assert "carto hook session-catchup" in jobs["SessionStart"][0]
    assert "carto hook prompt-capture --host copilot" in jobs["UserPromptSubmit"][0]
    assert "carto hook file-update" in jobs["Stop"][0]
    assert "carto hook session-summarise" in jobs["SessionEnd"][0]


def test_every_entry_is_the_vscode_schema_with_a_windows_form():
    config = skills.generate_copilot_hooks_config()
    for entries in config["hooks"].values():
        for entry in entries:
            assert entry["type"] == "command"
            assert "matcher" not in entry  # VS Code ignores it; the CLI format has none
            assert "carto hook" in entry["windows"]
            assert entry["timeout"] <= 30


def test_no_copilot_event_prints_to_stdout():
    """session-status is the one event that prints, and it is not wired here."""
    config = skills.generate_copilot_hooks_config()
    assert "session-status" not in json.dumps(config)


def test_install_writes_the_file_and_leaves_claude_alone(repo):
    path = skills.install_copilot_hooks(repo)

    assert path == repo / ".github" / "hooks" / "cartograph.json"
    assert json.loads(path.read_text()) == skills.generate_copilot_hooks_config()
    assert not (repo / ".claude").exists()


def test_install_overwrites_only_its_own_file(repo):
    other = repo / ".github" / "hooks" / "team.json"
    other.parent.mkdir(parents=True)
    other.write_text('{"hooks": {}}')

    skills.install_copilot_hooks(repo)
    skills.install_copilot_hooks(repo)

    assert other.read_text() == '{"hooks": {}}'
    assert sorted(p.name for p in other.parent.iterdir()) == ["cartograph.json", "team.json"]


def test_the_generated_capture_command_runs_end_to_end(tmp_path):
    """The real shell line with the real payload, as each host would run it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "carto"
    shim.write_text(f'#!/bin/sh\nexec {sys.executable} -m cartograph "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    command = skills.generate_copilot_hooks_config()["hooks"]["UserPromptSubmit"][0]["command"]
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
    }
    env.pop("COPILOT_CLI", None)

    for host_env, text in (({}, "Asked from Copilot Chat in VS Code"),
                           ({"COPILOT_CLI": "1"}, "Asked from the Copilot CLI instead")):
        result = subprocess.run(
            ["/bin/sh", "-c", command], cwd=repo,
            input=json.dumps(_vscode_payload(text, session=text)),
            capture_output=True, text=True, timeout=120, env={**env, **host_env},
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == ""

    hosts = {row["body"]: row["platform_source"] for row in _rows(repo, "prompts")}
    assert hosts == {
        "Asked from Copilot Chat in VS Code": "copilot-chat",
        "Asked from the Copilot CLI instead": "copilot-cli",
    }


def test_the_hook_finds_carto_where_the_extension_put_it(tmp_path):
    """VS Code runs a Chat hook in the extension host's environment, which is
    not a terminal's: the PATH entry the extension adds for terminals is not
    there. Measured on 2026-09-29 — without this every Chat hook exited at the
    guard, captured nothing, and said nothing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    home = tmp_path / "home"
    launcher_dir = home / ".cartograph" / "bin"
    launcher_dir.mkdir(parents=True)
    shim = launcher_dir / "carto"
    shim.write_text(f'#!/bin/sh\nexec {sys.executable} -m cartograph "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    command = skills.generate_copilot_hooks_config()["hooks"]["UserPromptSubmit"][0]["command"]
    env = {
        # Deliberately no launcher dir on PATH, only what git and sh need.
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
    }

    result = subprocess.run(
        ["/bin/sh", "-c", command], cwd=repo,
        input=json.dumps(_vscode_payload("Found through the default launcher dir")),
        capture_output=True, text=True, timeout=120, env=env,
    )

    assert result.returncode == 0, result.stderr
    assert [row["body"] for row in _rows(repo, "prompts")] == [
        "Found through the default launcher dir"
    ]
