"""Summaries that know what was concluded, recall by id, and an honest cost line.

From the claude-mem comparison of 2026-09-29 (commit ade13f3). claude-mem
condenses every tool call with a separate model call and reports a savings
figure that compares the compression model's spend against a full-read
estimate of what it wrote. Cartograph spends the user's Copilot quota on every
summary, so it stays at one summary per session — but feeds that summary each
turn's final assistant reply as well as the prompt, and reports only what it
can count.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cartograph import hook
from cartograph.mem import ingest, store, summarise, sync


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (hook.REENTRY_MARKER, summarise.SUMMARISE_MARKER, "COPILOT_CLI",
                 "CARTO_SUMMARY_MODEL"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _mirror_chat(repo: Path, session: str, turns: list[tuple[str, str]]) -> None:
    """A chatSessions log in the mirror, the shape VS Code writes."""
    requests = [
        {"requestId": f"r{i}", "message": {"text": prompt},
         "response": [{"kind": "mcpServersStarting"}, {"value": reply[:10]},
                      {"value": reply[10:]}]}
        for i, (prompt, reply) in enumerate(turns)
    ]
    mirror = sync.mirror_dir(repo)
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / f"{session}.jsonl").write_text(
        json.dumps({"kind": 0, "v": {"sessionId": session, "requests": requests}}) + "\n",
        encoding="utf-8",
    )


def _capture(repo: Path, session: str, *texts: str) -> None:
    for text in texts:
        assert ingest.capture(repo, {"session_id": session, "prompt": text}, host="copilot-chat")


def _stub_host(monkeypatch, answer):
    seen: list[str] = []

    def fake(host, text):
        seen.append(text)
        return answer

    monkeypatch.setattr(summarise, "call_host", fake)
    monkeypatch.setattr(summarise, "available_hosts", lambda prefer=None: [summarise.HOSTS[0]])
    return seen


def _meta(repo: Path, key: str):
    with store.MemoryStore(store.db_path(repo, create=False)) as memory:
        return memory.get_meta(key)


# --- the replies reach the brief ---------------------------------------------


def test_each_turns_final_reply_reaches_the_brief(repo, monkeypatch):
    turns = [
        ("Should io.load raise or return None for a missing file?",
         "Raise FileNotFoundError; returning None hides the failure."),
        ("Agreed, we raise. Returning None crashed callers before.",
         "Understood: io.load raises FileNotFoundError."),
    ]
    _capture(repo, "s1", *(p for p, _ in turns))
    _mirror_chat(repo, "s1", turns)
    sent = _stub_host(monkeypatch, "TITLE: t\nDECIDED: raise")

    summarise.summarise(repo, session="s1")

    assert "Raise FileNotFoundError; returning None hides the failure." in sent[0]
    assert "ASSISTANT" in sent[0]


def test_the_brief_separates_decided_from_proposed():
    text = summarise.brief([{"body": "p1"}, {"body": "p2"}], ["r1", None])
    assert "PROPOSED" in text
    assert "only" in text.split("DECIDED:")[1].split("\n")[0]


def test_without_logs_the_brief_is_prompts_only(repo, monkeypatch):
    _capture(repo, "s1", "First prompt with no log behind it", "Second prompt, same")
    sent = _stub_host(monkeypatch, "TITLE: t")
    summarise.summarise(repo, session="s1")
    assert "ASSISTANT" not in sent[0].split("SESSION:")[1]


def test_each_reply_is_clipped_in_the_brief():
    text = summarise.brief([{"body": "p"}], ["z" * 5000])
    assert text.count("z") == summarise.MAX_REPLY_CHARS
    assert "[…]" in text


def test_replies_are_never_stored_as_prompts(repo, monkeypatch):
    turns = [("A prompt long enough to capture", "A reply that must not be searchable")]
    _capture(repo, "s1", turns[0][0])
    _mirror_chat(repo, "s1", turns)
    sync.sync(repo, user_dirs=[], copilot_dir=repo / "no-copilot")
    with store.MemoryStore(store.db_path(repo, create=False)) as memory:
        bodies = [r[0] for r in memory._conn.execute("SELECT body FROM observations")]  # noqa: SLF001
    assert "A reply that must not be searchable" not in bodies


# --- which model ---------------------------------------------------------------


def test_copilot_is_asked_to_choose_its_own_model():
    """`auto` picks by task, costs 10% less on paid plans, and never picks a
    model an administrator blocked — a pinned name could be blocked and turn
    every summary structural without a word."""
    copilot = next(h for h in summarise.HOSTS if h.binary == "copilot")
    argv = copilot.argv("brief")
    assert argv[argv.index("--model") + 1] == "auto"


def test_the_model_can_be_pinned(monkeypatch):
    monkeypatch.setenv("CARTO_SUMMARY_MODEL", "gpt-6-luna")
    copilot = next(h for h in summarise.HOSTS if h.binary == "copilot")
    argv = copilot.argv("brief")
    assert argv[argv.index("--model") + 1] == "gpt-6-luna"


# --- what it cost --------------------------------------------------------------


def test_host_calls_are_counted_including_failed_ones(repo, monkeypatch):
    """A failed call still spent quota, so it still counts."""
    _capture(repo, "s1", "First prompt of session one", "Second prompt of session one")
    _stub_host(monkeypatch, None)  # fails -> structural
    summarise.summarise(repo, session="s1")
    assert _meta(repo, summarise.COST_HOST_CALLS) == "1"


def test_the_raw_size_of_a_summarised_session_is_recorded(repo, monkeypatch):
    turns = [("Prompt one of the recorded session", "Reply one"),
             ("Prompt two of the recorded session", "Reply two")]
    _capture(repo, "s1", *(p for p, _ in turns))
    _mirror_chat(repo, "s1", turns)
    _stub_host(monkeypatch, "TITLE: t")
    summarise.summarise(repo, session="s1")
    expected = sum(len(p) + len(r) for p, r in turns)
    assert _meta(repo, summarise.COST_RAW_CHARS + "s1") == str(expected)


# --- mem show, and the status line -------------------------------------------


def _cli(repo: Path, *args: str) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, "-m", "cartograph", "mem", *args, "--repo", str(repo),
         "--format", "json"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
    )
    return proc.returncode, json.loads(proc.stdout)


def test_mem_show_returns_the_full_bodies_by_id(repo):
    long_text = "Why we chose SQLite. " + "detail " * 200
    _capture(repo, "s1", long_text)
    _, found = _cli(repo, "search", "--query", "SQLite")
    obs_id = found["data"]["items"][0]["id"]

    code, shown = _cli(repo, "show", "--id", obs_id)

    assert code == 0
    assert shown["data"]["items"][0]["body"] == long_text.strip() or \
        shown["data"]["items"][0]["body"].startswith("Why we chose SQLite.")
    assert len(shown["data"]["items"][0]["body"]) > len(found["data"]["items"][0]["snippet"])


def test_mem_show_names_ids_it_did_not_find(repo):
    _capture(repo, "s1", "Anything at all to create the store")
    code, shown = _cli(repo, "show", "--id", "0000000000000000")
    assert code == 0
    assert shown["data"]["missing"] == ["0000000000000000"]


def test_status_reports_what_memory_cost_and_what_it_replaced(repo, monkeypatch):
    turns = [("Prompt one of a summarised session", "Reply one here"),
             ("Prompt two of a summarised session", "Reply two here")]
    _capture(repo, "s1", *(p for p, _ in turns))
    _mirror_chat(repo, "s1", turns)
    _stub_host(monkeypatch, "TITLE: t")
    summarise.summarise(repo, session="s1")
    _cli(repo, "search", "--query", "summarised")

    _, status = _cli(repo, "status")

    assert status["data"]["memory_cost"].startswith("1 summary call(s) to Copilot")
    assert "1 recall(s) served" in status["data"]["memory_cost"]
    assert "chars/4" in status["data"]["vs_raw_logs"]
