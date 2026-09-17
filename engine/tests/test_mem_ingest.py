"""Hook ingestion: what gets recorded, what is refused, and what must not fail.

Two properties carry this file. The first is that capture obeys the hook
protocol — exit 0, no envelope, no exception out of the hook path, whatever
the host sends. The second is the budget: an ingestion path that records
everything is how a store becomes unsearchable, so the refusals are tested as
carefully as the writes.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cartograph import hook, skills
from cartograph.mem import ingest
from cartograph.mem import store as mem_store


#: What Claude Code actually sends a UserPromptSubmit hook on stdin. Field
#: names taken from live payloads, not from memory.
def _claude_payload(prompt: str, session: str = "sess-1") -> dict:
    return {
        "session_id": session,
        "transcript_path": "/Users/someone/.claude/projects/x/abc.jsonl",
        "cwd": "/Users/someone/work/repo",
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
    }


def _feed(monkeypatch, payload) -> None:
    """Put a host payload on stdin the way a host pipes it in."""
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))


def _observations(repo_root: Path) -> list[dict]:
    path = mem_store.db_path(repo_root, create=False)
    if not path.exists():
        return []
    with mem_store.MemoryStore(path) as memory:
        rows = memory._conn.execute(
            "SELECT id, title, body, kind, doc_type, session, platform_source, "
            "summary_source FROM observations ORDER BY created_at"
        ).fetchall()
    return [dict(row) for row in rows]


# --- The protocol is still the hook protocol -------------------------------


def test_capture_exits_zero_and_emits_nothing(tmp_path, monkeypatch, capsys):
    """Whatever a UserPromptSubmit hook prints is prepended to the prompt."""
    _feed(monkeypatch, _claude_payload("Wire hook ingestion into the memory store"))

    assert hook.run("UserPromptSubmit", repo=str(tmp_path)) == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize(
    "stdin_text",
    ["", "not json at all", "[]", '"a string"', "null", '{"prompt": null}'],
)
def test_malformed_payloads_exit_zero_silently(stdin_text, tmp_path, monkeypatch, capsys):
    _feed(monkeypatch, stdin_text)

    assert hook.run("prompt-capture", repo=str(tmp_path)) == 0
    assert capsys.readouterr().out == ""
    assert _observations(tmp_path) == []


def test_a_terminal_stdin_is_not_read(tmp_path, monkeypatch):
    """`carto hook prompt-capture` typed at a prompt must not block on read()."""

    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

        def read(self, *_args):  # pragma: no cover - the failure this guards
            raise AssertionError("read() on a TTY would block until EOF")

    monkeypatch.setattr(sys, "stdin", _Tty())
    assert hook.run("prompt-capture", repo=str(tmp_path)) == 0


def test_a_store_that_cannot_be_opened_is_not_an_error(tmp_path, monkeypatch, capsys):
    def _explode(*_args, **_kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(mem_store, "db_path", _explode)
    _feed(monkeypatch, _claude_payload("A prompt long enough to be recorded"))

    assert hook.run("UserPromptSubmit", repo=str(tmp_path)) == 0
    assert capsys.readouterr().out == ""


def test_prompt_capture_resolves_like_every_host_spells_it():
    for spelling in ("UserPromptSubmit", "userPromptSubmit", "user_prompt_submit",
                     "beforeSubmitPrompt", "prompt-capture"):
        assert hook.resolve_event(spelling) == "prompt-capture"


# --- What is recorded ------------------------------------------------------


def test_a_prompt_becomes_one_observation(tmp_path, monkeypatch):
    prompt = "Wire hook ingestion so observations are captured automatically"
    _feed(monkeypatch, _claude_payload(prompt))

    hook.run("UserPromptSubmit", repo=str(tmp_path), host="claude-code")

    rows = _observations(tmp_path)
    assert len(rows) == 1
    assert rows[0]["title"] == prompt
    assert rows[0]["body"] == prompt
    assert rows[0]["kind"] == "prompt"
    assert rows[0]["doc_type"] == "prompts"
    assert rows[0]["session"] == "sess-1"
    assert rows[0]["platform_source"] == "claude-code"


def test_what_is_recorded_is_verbatim(tmp_path, monkeypatch):
    """The field exists to tell a copy from a summary. This path only copies."""
    _feed(monkeypatch, _claude_payload("Record this prompt exactly as written"))

    hook.run("UserPromptSubmit", repo=str(tmp_path))

    assert _observations(tmp_path)[0]["summary_source"] == "verbatim"


def test_the_store_is_created_on_first_capture(tmp_path, monkeypatch):
    assert not (tmp_path / ".cartograph").exists()
    _feed(monkeypatch, _claude_payload("A first prompt in a fresh checkout"))

    hook.run("UserPromptSubmit", repo=str(tmp_path))

    assert mem_store.db_path(tmp_path, create=False).exists()


def test_a_captured_observation_comes_back_out_of_search(tmp_path, monkeypatch):
    _feed(monkeypatch, _claude_payload("Investigate the flaky cursor pagination test"))
    hook.run("UserPromptSubmit", repo=str(tmp_path))

    with mem_store.MemoryStore(mem_store.db_path(tmp_path)) as memory:
        items, mode, _ = memory.search(query="cursor pagination")

    assert [item["title"] for item in items] == [
        "Investigate the flaky cursor pagination test"
    ]
    assert mode in ("fts", "hybrid")


# --- The budget ------------------------------------------------------------


def test_a_refused_payload_creates_no_store(tmp_path, monkeypatch):
    """Refusal is decided before the store is touched.

    Otherwise a repository where nothing was ever worth recording would still
    grow a `.cartograph` directory on the first prompt.
    """
    _feed(monkeypatch, _claude_payload("ok"))

    hook.run("UserPromptSubmit", repo=str(tmp_path))

    assert not (tmp_path / ".cartograph").exists()


def test_an_acknowledgement_is_not_an_observation():
    for text in ("ok", "yes", "continue", "go ahead", "y", "do it"):
        assert ingest.observation_from({"prompt": text}) is None


def test_an_event_with_no_prompt_is_refused():
    """PostToolUse and SessionEnd payloads reach this if a host is miswired."""
    assert ingest.observation_from(
        {"hook_event_name": "PostToolUse", "tool_name": "Edit",
         "tool_input": {"file_path": "/repo/a.py"}, "tool_response": {"ok": True}}
    ) is None
    assert ingest.observation_from({"hook_event_name": "SessionEnd", "reason": "clear"}) is None


def test_a_pasted_wall_of_text_is_cut_and_says_so():
    body = ingest.observation_from({"prompt": "x" * 10_000})["body"]

    assert len(body) < 10_000
    assert body.startswith("x" * ingest.MAX_BODY_CHARS)
    assert "truncated" in body


def test_the_title_is_the_first_line_bounded():
    shaped = ingest.observation_from({"prompt": "Fix the parser\n\nHere is the traceback…"})
    assert shaped["title"] == "Fix the parser"

    long_first_line = ingest.observation_from({"prompt": "word " * 200})
    assert len(long_first_line["title"]) <= ingest.MAX_TITLE_CHARS


def test_a_session_stops_recording_at_the_cap(tmp_path, monkeypatch):
    for index in range(ingest.SESSION_CAP + 5):
        _feed(monkeypatch, _claude_payload(f"Prompt number {index} in this session"))
        hook.run("UserPromptSubmit", repo=str(tmp_path))

    assert len(_observations(tmp_path)) == ingest.SESSION_CAP


def test_the_cap_is_per_session_not_per_store(tmp_path, monkeypatch):
    for index in range(ingest.SESSION_CAP):
        _feed(monkeypatch, _claude_payload(f"Prompt number {index}, first session"))
        hook.run("UserPromptSubmit", repo=str(tmp_path))
    _feed(monkeypatch, _claude_payload("A prompt from a brand new session", "sess-2"))
    hook.run("UserPromptSubmit", repo=str(tmp_path))

    assert len(_observations(tmp_path)) == ingest.SESSION_CAP + 1


def test_hosts_that_spell_the_fields_differently_still_capture():
    """Copilot-family and editor payloads name the same two things otherwise."""
    assert ingest.observation_from(
        {"conversation_id": "c1", "query": "What changed in the auth module?"}
    )["session"] == "c1"
    assert ingest.observation_from(
        {"sessionId": "s1", "message": "Summarise the failing integration test"}
    )["session"] == "s1"


# --- Re-entrancy -----------------------------------------------------------


def test_a_hook_inside_a_hook_records_nothing(tmp_path, monkeypatch):
    """Capture adds an edge to the loop the marker already guards.

    A capture that ran inside a Cartograph-launched session would record
    Cartograph's own activity as if a person had asked for it.
    """
    monkeypatch.setenv(hook.REENTRY_MARKER, "1")
    _feed(monkeypatch, _claude_payload("This prompt must not be recorded"))

    assert hook.run("UserPromptSubmit", repo=str(tmp_path)) == 0

    assert _observations(tmp_path) == []
    assert not (tmp_path / ".cartograph").exists(), "capture created a store under the marker"


def test_capture_starts_no_process_that_could_fire_a_hook(tmp_path, monkeypatch):
    """The write is in process; nothing here spawns anything.

    A subprocess would cost more than the insert it wrapped, and every
    spawned process is another chance to re-enter the hooks.
    """
    monkeypatch.setattr(
        hook, "spawn_detached", lambda *a, **k: pytest.fail("capture spawned a process")
    )
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: pytest.fail("capture spawned a process")
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("capture spawned a process")
    )
    _feed(monkeypatch, _claude_payload("A prompt recorded without a subprocess"))

    assert hook.run("UserPromptSubmit", repo=str(tmp_path)) == 0
    assert len(_observations(tmp_path)) == 1


# --- The generated host config ---------------------------------------------


def test_claude_config_wires_capture_through_the_shared_command():
    config = skills.generate_hooks_config(Path("/repo"))
    command = config["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]

    assert command == skills.hook_command(
        "prompt-capture", host="claude-code", reads_payload=True
    )
    assert "carto hook prompt-capture" in command
    assert command.index("git rev-parse --git-dir") < command.index("carto hook")


def test_the_capture_command_does_not_throw_the_payload_away():
    """`cat >/dev/null` first would discard the only input this event has."""
    command = skills.hook_command("prompt-capture", reads_payload=True)

    assert not command.startswith("cat >/dev/null")
    # Still drained when a guard short-circuits, so the host is never left
    # writing into a pipe nobody reads.
    assert command.rstrip().endswith("cat >/dev/null || true")


def test_the_other_events_still_drain_first():
    for event in ("session-status", "file-update"):
        assert skills.hook_command(event).startswith("cat >/dev/null || true; ")


def test_the_generated_capture_command_runs_end_to_end(tmp_path):
    """The real shell line, with the real payload, against a real git repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    # Stands in for the console script, which is only present in an installed
    # environment; `carto` resolving to this interpreter is what the guard in
    # the generated line is testing for.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "carto"
    shim.write_text(
        f'#!/bin/sh\nexec {sys.executable} -m cartograph "$@"\n', encoding="utf-8"
    )
    shim.chmod(0o755)

    command = skills.generate_hooks_config(repo)["hooks"]["UserPromptSubmit"][0][
        "hooks"
    ][0]["command"]
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        cwd=repo,
        input=json.dumps(_claude_payload("End to end through the generated shell line")),
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
        },
    )

    assert result.returncode == 0
    assert result.stdout == ""
    # The shell resolves the root itself; on macOS that is the /private form
    # of the temporary directory, and the store landed there.
    root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=repo,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    )
    assert [row["title"] for row in _observations(root)] == [
        "End to end through the generated shell line"
    ]
