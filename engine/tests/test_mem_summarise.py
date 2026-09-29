"""``carto mem summarise`` — the budget, the guard, and the two sources.

The pins here are mostly about what must NOT happen. A structural summary must
not be recorded as a host-agent one, because ``summary_source`` is the only
thing telling an agent how far to trust the prose. A summarisation must not run
inside a hook's critical path, and must not be able to start another one. And
the verbatim prompt rows must survive it, because they are the evidence the
summary was derived from.

No test here calls a real host agent. Every host interaction goes through a
stub argv, so the suite is deterministic and spends nobody's quota; the real
invocation was verified by hand against `copilot --help` and `claude --help` and
then run. What the suite can hold is that the argv is *built* from those flags
and that every failure mode lands on the fallback.
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


# --- helpers ---------------------------------------------------------------


def _capture(repo: Path, session: str, *texts: str, host: str = "claude-code") -> None:
    """Record prompts the way the hook does, so the rows are the real shape."""
    for text in texts:
        assert ingest.capture(
            repo, {"session_id": session, "prompt": text}, host=host
        ), text


def _rows(repo: Path, doc_type: str) -> list[dict]:
    path = store.db_path(repo, create=False)
    with store.MemoryStore(path) as memory:
        return [
            dict(row)
            for row in memory._conn.execute(  # noqa: SLF001 — asserting storage
                "SELECT * FROM observations WHERE doc_type = ? ORDER BY created_at",
                (doc_type,),
            ).fetchall()
        ]


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


@pytest.fixture(autouse=True)
def _no_inherited_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite must not inherit a guard from whatever launched it."""
    monkeypatch.delenv(summarise.SUMMARISE_MARKER, raising=False)
    monkeypatch.delenv(hook.REENTRY_MARKER, raising=False)


#: Characters the brief's own wording contributes, so a test counting them in
#: the prompts is not counting the template's too.
_BRIEF = {"x": summarise._BRIEF.count("x")}


def _stub_host(monkeypatch: pytest.MonkeyPatch, answer: str | None) -> list[list[str]]:
    """Replace the host call, returning the argv list it was asked to run."""
    seen: list[list[str]] = []

    def fake(host: summarise.Host, text: str) -> str | None:
        seen.append(host.argv(text))
        return answer

    monkeypatch.setattr(summarise, "call_host", fake)
    monkeypatch.setattr(summarise, "available_hosts", lambda prefer=None: [summarise.HOSTS[0]])
    return seen


# --- the unit is a session, and the prompts are left alone -----------------


def test_one_sessions_row_per_session_and_the_prompts_survive(repo, monkeypatch):
    """The whole design in one assertion: synthesis is ADDED, not substituted."""
    _capture(repo, "s1", "Wire the store into capture", "Back out the inline host call")
    _stub_host(monkeypatch, "TITLE: Wiring capture\nWORKED ON: the store\nDEAD ENDS: none")

    result = summarise.summarise(repo, session="s1")

    prompts = _rows(repo, "prompts")
    sessions = _rows(repo, "sessions")
    assert len(prompts) == 2
    assert [row["summary_source"] for row in prompts] == ["verbatim", "verbatim"]
    assert len(sessions) == 1
    assert sessions[0]["kind"] == "session"
    assert sessions[0]["session"] == "s1"
    assert result["observation"]["id"] == sessions[0]["id"]
    # The summary is a different row, never one of the prompts rewritten.
    assert sessions[0]["id"] not in {row["id"] for row in prompts}


def test_host_answer_is_recorded_as_host_agent(repo, monkeypatch):
    _capture(repo, "s1", "First real prompt here", "Second real prompt here")
    _stub_host(monkeypatch, "TITLE: A title\nWORKED ON: things\nDEAD ENDS: none")

    result = summarise.summarise(repo, session="s1")

    assert result["summary_source"] == "host-agent"
    assert result["observation"]["title"] == "A title"
    assert "WORKED ON: things" in _rows(repo, "sessions")[0]["body"]
    assert "fallback_reason" not in result


def test_a_structural_summary_is_never_labelled_host_agent(repo, monkeypatch):
    """T07's explicit requirement, and the reason `summary_source` exists."""
    _capture(repo, "s1", "First real prompt here", "Second real prompt here")
    _stub_host(monkeypatch, None)

    result = summarise.summarise(repo, session="s1")

    assert result["summary_source"] == "structural"
    assert result["host"] is None
    assert _rows(repo, "sessions")[0]["summary_source"] == "structural"
    # And it says so in the prose, not only in a field a reader may not look at.
    assert "no host agent wrote this" in _rows(repo, "sessions")[0]["body"]
    assert result["fallback_reason"]


def test_structural_body_indexes_every_prompt_in_order(repo):
    _capture(repo, "s1", "Alpha the first request", "Beta the second request",
             "Gamma the third request")

    summarise.summarise(repo, session="s1", use_host=False)

    body = _rows(repo, "sessions")[0]["body"]
    assert body.index("Alpha") < body.index("Beta") < body.index("Gamma")


def test_structural_summary_is_deterministic(repo):
    _capture(repo, "s1", "Alpha the first request", "Beta the second request")
    path = store.db_path(repo, create=False)
    with store.MemoryStore(path) as memory:
        prompts = memory.session_documents("s1", doc_type="prompts")

    assert summarise.structural_summary("s1", prompts) == summarise.structural_summary(
        "s1", prompts
    )


# --- the budget ------------------------------------------------------------


def test_a_session_is_never_summarised_twice(repo, monkeypatch):
    _capture(repo, "s1", "First real prompt here", "Second real prompt here")
    calls = _stub_host(monkeypatch, "TITLE: t\nWORKED ON: w")

    summarise.summarise(repo, session="s1")
    again = summarise.summarise(repo, session="s1")

    assert again["already_summarised"] is True
    assert again["observation"] is None
    assert len(_rows(repo, "sessions")) == 1
    assert len(calls) == 1, "the second call spent an inference it did not need"


def test_a_single_prompt_session_is_not_summarised(repo, monkeypatch):
    _capture(repo, "s1", "The only prompt in this session")
    calls = _stub_host(monkeypatch, "TITLE: t")

    result = summarise.summarise(repo, session="s1")

    assert result["observation"] is None
    assert result["prompts_available"] == 1
    assert calls == [], "summarising one prompt is the reworded copy this rejects"
    assert _rows(repo, "sessions") == []


def test_only_the_most_recent_prompts_reach_the_brief(repo, monkeypatch):
    _capture(repo, "s1", *[f"Prompt number {i} of the session" for i in range(
        summarise.MAX_PROMPTS + 5
    )])
    calls = _stub_host(monkeypatch, "TITLE: t")

    result = summarise.summarise(repo, session="s1")

    assert result["prompts_summarised"] == summarise.MAX_PROMPTS
    sent = calls[0][2]
    assert "Prompt number 0 of" not in sent
    assert f"Prompt number {summarise.MAX_PROMPTS + 4} of" in sent


def test_each_prompt_is_clipped_in_the_brief(repo):
    long_prompt = "x" * (ingest.MAX_BODY_CHARS - 1)
    path = store.db_path(repo, create=True)
    with store.MemoryStore(path) as memory:
        for index in range(2):
            memory.add(
                project="p", title=f"t{index}", body=long_prompt,
                session="s1", doc_type="prompts",
            )
        prompts = memory.session_documents("s1", doc_type="prompts")

    text = summarise.brief(prompts)

    assert "[…]" in text
    # Two prompts of 4,000 characters would put 8,000 in the brief; the cap
    # makes it 1,000. The two spare are the template's own ("exactly").
    assert text.count("x") == 2 * summarise.MAX_PROMPT_CHARS + _BRIEF["x"]


def test_the_brief_asks_for_dead_ends(repo):
    """The one section with no substitute anywhere else in a repository."""
    assert "DEAD ENDS" in summarise.brief([{"body": "anything"}])


def test_a_runaway_host_answer_is_clipped(repo, monkeypatch):
    _capture(repo, "s1", "First real prompt here", "Second real prompt here")
    _stub_host(monkeypatch, "TITLE: t\n" + "y" * (ingest.MAX_BODY_CHARS * 3))

    summarise.summarise(repo, session="s1")

    body = _rows(repo, "sessions")[0]["body"]
    assert len(body) < ingest.MAX_BODY_CHARS + 60
    assert "truncated by carto summarise" in body


# --- recursion -------------------------------------------------------------


def test_the_depth_cap_forces_the_structural_path(repo, monkeypatch):
    _capture(repo, "s1", "First real prompt here", "Second real prompt here")
    calls = _stub_host(monkeypatch, "TITLE: t")
    monkeypatch.setenv(summarise.SUMMARISE_MARKER, "1")

    result = summarise.summarise(repo, session="s1")

    assert calls == [], "a summarisation started another one"
    assert result["summary_source"] == "structural"
    assert "already running" in result["fallback_reason"]


def test_the_hook_marker_does_not_block_the_summariser(repo, monkeypatch):
    """The trap worth a test of its own.

    ``spawn_detached`` sets ``CARTO_HOOK_ACTIVE`` in the child it launches, and
    the child it launches for this event IS the summariser. Reusing that marker
    as the depth cap would make every hook-driven summary structural while
    every hand-run one was host-agent — a difference nothing would explain.
    """
    _capture(repo, "s1", "First real prompt here", "Second real prompt here")
    calls = _stub_host(monkeypatch, "TITLE: t")
    monkeypatch.setenv(hook.REENTRY_MARKER, "1")

    result = summarise.summarise(repo, session="s1")

    assert len(calls) == 1
    assert result["summary_source"] == "host-agent"


def test_both_markers_are_set_in_the_hosts_environment(monkeypatch):
    """What the host's own hooks see, which is what stops the cycle."""
    monkeypatch.delenv(hook.REENTRY_MARKER, raising=False)
    monkeypatch.delenv(summarise.SUMMARISE_MARKER, raising=False)

    env = summarise._host_env()

    assert env[hook.REENTRY_MARKER] == "1"
    assert env[summarise.SUMMARISE_MARKER] == "1"


def test_the_marker_reaches_a_grandchild_process():
    """Inheritance is the whole mechanism, so it is asserted, not assumed.

    The host agent sits between this process and the hooks it runs, so the
    marker has to survive two levels of spawn to cover the tree the guard
    claims to cover.
    """
    inner = (
        "import os,subprocess,sys;"
        "subprocess.run([sys.executable,'-c',"
        "\"import os;print(os.environ.get('CARTO_SUMMARISE_ACTIVE'),"
        "os.environ.get('CARTO_HOOK_ACTIVE'))\"])"
    )
    proc = subprocess.run(
        [sys.executable, "-c", inner],
        env=summarise._host_env(), capture_output=True, text=True, timeout=60,
    )

    assert proc.stdout.strip() == "1 1"


def test_every_host_invocation_is_recorded_as_no_hooks_or_not_at_all():
    """A host with no such flag must say so rather than imply it has one.

    T07 treats the host's no-hooks flag as one of three parts of the guard. It
    cannot be passed where it does not exist, and pretending otherwise would
    leave the env marker carrying the whole load with nothing recording that.
    """
    for host in summarise.HOSTS:
        argv = host.argv("brief")
        assert argv[0] == host.binary
        assert "brief" in argv
        if host.no_hooks_flag:
            assert host.no_hooks_flag in argv


def test_claude_is_invoked_with_safe_mode_and_no_tools():
    """Pins the two flags that were checked against `claude --help` and run.

    ``--bare`` also skips hooks and was rejected: it restricts auth to an API
    key, which would break every subscription install.
    """
    claude = next(host for host in summarise.HOSTS if host.binary == "claude")
    argv = claude.argv("brief")

    assert argv[:3] == ["claude", "-p", "brief"]
    assert "--safe-mode" in argv
    assert "--bare" not in argv
    assert argv[argv.index("--tools") + 1] == ""


def test_copilot_is_invoked_non_interactively_and_offline_safe():
    copilot = next(host for host in summarise.HOSTS if host.binary == "copilot")
    argv = copilot.argv("brief")

    assert argv[:3] == ["copilot", "-p", "brief"]
    assert "--silent" in argv
    # A summarisation must not be the thing that downloads a CLI update on a
    # machine whose whole premise is default-deny egress.
    assert "--no-auto-update" in argv
    assert "--disable-builtin-mcps" in argv


def test_the_session_host_is_preferred(monkeypatch):
    monkeypatch.setattr(summarise.shutil, "which", lambda binary: f"/usr/bin/{binary}")

    order = [host.name for host in summarise.available_hosts(prefer="claude-code")]

    assert order[0] == "claude-code"
    assert set(order) == {host.name for host in summarise.HOSTS}


def test_no_host_on_path_is_not_an_error(repo, monkeypatch):
    """The acceptance test's machine. Silence, and a structural summary."""
    _capture(repo, "s1", "First real prompt here", "Second real prompt here")
    monkeypatch.setattr(summarise.shutil, "which", lambda binary: None)

    result = summarise.summarise(repo, session="s1")

    assert result["summary_source"] == "structural"
    assert "no host agent CLI on PATH" in result["fallback_reason"]


def test_a_failing_host_falls_through_to_the_next_then_to_structural(repo, monkeypatch):
    _capture(repo, "s1", "First real prompt here", "Second real prompt here")
    tried: list[str] = []

    monkeypatch.setattr(summarise.shutil, "which", lambda binary: f"/usr/bin/{binary}")
    monkeypatch.setattr(
        summarise, "call_host",
        lambda host, text: (tried.append(host.binary), None)[1],
    )

    result = summarise.summarise(repo, session="s1")

    # Every installed host is tried once — claude first here, because that is
    # the host that recorded the prompts.
    assert tried == ["claude", "copilot"]
    assert set(tried) == {host.binary for host in summarise.HOSTS}
    assert result["summary_source"] == "structural"
    assert "failed or timed out" in result["fallback_reason"]


def test_a_host_timeout_is_a_fallback_not_an_exception(monkeypatch):
    def boom(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

    monkeypatch.setattr(summarise.subprocess, "run", boom)

    assert summarise.call_host(summarise.HOSTS[0], "brief") is None


def test_a_host_that_exits_nonzero_is_not_trusted(monkeypatch):
    monkeypatch.setattr(
        summarise.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "half an answer", "boom"),
    )

    assert summarise.call_host(summarise.HOSTS[0], "brief") is None


def test_an_empty_host_answer_is_not_a_summary(monkeypatch):
    monkeypatch.setattr(
        summarise.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "   \n ", ""),
    )

    assert summarise.call_host(summarise.HOSTS[0], "brief") is None


def test_the_host_is_never_given_a_stdin_or_the_checkout(monkeypatch):
    seen: dict = {}

    def record(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, "TITLE: t", "")

    monkeypatch.setattr(summarise.subprocess, "run", record)
    summarise.call_host(summarise.HOSTS[0], "brief")

    assert seen["stdin"] == subprocess.DEVNULL
    assert seen["timeout"] == summarise.HOST_TIMEOUT_SECONDS
    assert "cwd" not in seen


# --- parsing the answer ----------------------------------------------------


def test_the_title_line_is_lifted_out_of_the_body():
    title, body = summarise.split_title("TITLE: The thing\nWORKED ON: stuff")

    assert title == "The thing"
    assert body == "WORKED ON: stuff"
    assert "TITLE:" not in body


def test_a_missing_title_line_keeps_the_host_body(repo, monkeypatch):
    """An inference already paid for is not thrown away over its formatting."""
    _capture(repo, "s1", "First real prompt here", "Second real prompt here")
    _stub_host(monkeypatch, "WORKED ON: stuff\nDEAD ENDS: none")

    result = summarise.summarise(repo, session="s1")

    assert result["summary_source"] == "host-agent"
    assert result["observation"]["title"].startswith("Session s1:")
    assert "WORKED ON: stuff" in _rows(repo, "sessions")[0]["body"]


def test_a_long_host_title_is_bounded_like_every_other_title():
    title, _ = summarise.split_title("TITLE: " + "z" * 400)

    assert len(title) <= ingest.MAX_TITLE_CHARS


# --- the default session ---------------------------------------------------


def test_with_no_session_the_latest_one_is_summarised(repo, monkeypatch):
    _capture(repo, "older", "An older prompt in a finished session",
             "A second older prompt in it")
    _capture(repo, "newer", "A newer prompt in the current session",
             "A second newer prompt in it")
    _stub_host(monkeypatch, "TITLE: t")

    result = summarise.summarise(repo)

    assert result["session"] == "newer"


def test_an_empty_store_is_a_no_op_not_a_crash(repo):
    store.db_path(repo, create=True)
    with store.MemoryStore(store.db_path(repo)):
        pass

    result = summarise.summarise(repo)

    assert result["observation"] is None
    assert result["session"] is None


# --- the hook path ---------------------------------------------------------


def test_session_end_resolves_to_the_summarise_event():
    for spelling in ("SessionEnd", "sessionEnd", "session_end", "session-summarise"):
        assert hook.resolve_event(spelling) == "session-summarise", spelling


def test_stop_is_not_aliased_onto_it():
    """`Stop` fires at every turn boundary — its real payload carries
    `stop_hook_active` and `last_assistant_message`. Aliasing it would rewrite
    the summary after every reply."""
    assert hook.resolve_event("Stop") is None


def test_the_hook_spawns_and_does_not_summarise_inline(repo, monkeypatch):
    """Never in the critical path: the hook's own work is building an argv."""
    _capture(repo, "s1", "First real prompt here", "Second real prompt here")
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        hook, "spawn_detached",
        lambda argv, cwd=None: (spawned.append(list(argv)), True)[1],
    )
    monkeypatch.setattr(
        summarise, "call_host",
        lambda *_a, **_k: pytest.fail("the hook summarised inline"),
    )

    payload = json.dumps({
        "session_id": "s1", "hook_event_name": "SessionEnd", "reason": "other",
    })
    exit_code = hook.run("SessionEnd", repo=str(repo))  # no stdin: nothing to do
    assert exit_code == 0

    monkeypatch.setattr(hook, "hook_payload", lambda stream=None: json.loads(payload))
    assert hook.run("SessionEnd", repo=str(repo)) == 0
    assert spawned == [hook.summarise_argv(repo, "s1")]
    assert _rows(repo, "sessions") == []


def test_the_spawned_argv_names_the_session_and_the_repo(repo):
    argv = hook.summarise_argv(repo, "s1")

    assert argv[:5] == [sys.executable, "-m", "cartograph", "mem", "summarise"]
    assert argv[argv.index("--session") + 1] == "s1"
    assert argv[argv.index("--repo") + 1] == str(repo)


def test_no_session_id_means_no_spawn(repo, monkeypatch):
    _capture(repo, "s1", "First real prompt here", "Second real prompt here")
    monkeypatch.setattr(
        hook, "spawn_detached",
        lambda *_a, **_k: pytest.fail("spawned with nothing to summarise"),
    )
    monkeypatch.setattr(hook, "hook_payload", lambda stream=None: {"reason": "clear"})

    assert hook.run("SessionEnd", repo=str(repo)) == 0


def test_no_store_means_no_spawn(repo, monkeypatch):
    """A repository nobody has captured a prompt in must not pay an interpreter
    start on every session end."""
    monkeypatch.setattr(
        hook, "spawn_detached",
        lambda *_a, **_k: pytest.fail("spawned for a repository with no store"),
    )
    monkeypatch.setattr(hook, "hook_payload", lambda stream=None: {"session_id": "s1"})

    assert hook.run("SessionEnd", repo=str(repo)) == 0


def test_the_event_exits_zero_even_when_everything_fails(repo, monkeypatch):
    """The hook protocol's only hard rule. 2 would halt the session."""
    monkeypatch.setattr(
        hook, "hook_payload", lambda stream=None: {"session_id": "s1"}
    )
    monkeypatch.setattr(
        hook, "spawn_detached",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    store.db_path(repo, create=True).touch()

    assert hook.run("SessionEnd", repo=str(repo)) == 0


def test_the_generated_claude_config_wires_session_end():
    config = skills.generate_hooks_config(Path("/repo"))
    command = config["hooks"]["SessionEnd"][0]["hooks"][0]["command"]

    assert "carto hook session-summarise" in command
    # The payload carries the session id, so the drain must come after the
    # work, not before it — the same rule prompt-capture needed.
    assert command.index("carto hook") < command.index("cat >/dev/null")
    assert "command -v carto >/dev/null 2>&1 || exit 0" in command
    assert command.index("git rev-parse --git-dir") < command.index("carto hook")


def test_the_hook_module_still_never_reaches_for_the_envelope():
    """Carried forward: the new event must not have opened that door."""
    import ast

    tree = ast.parse(Path(hook.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported += [f"{node.module or ''}.{a.name}" for a in node.names]

    assert not [name for name in imported if "envelope" in name]


# --- the CLI surface -------------------------------------------------------


def _run_cli(*args: str) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, "-m", "cartograph", *args, "--format", "json"],
        capture_output=True, text=True, timeout=120,
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    return proc.returncode, json.loads(proc.stdout)


def test_the_command_is_in_the_catalogue():
    """Against the real output, not a parser rebuilt in the test.

    The catalogue is generated by introspecting the live parser, so the only
    thing that proves an agent can find the command is what the shipped binary
    actually prints.
    """
    code, doc = _run_cli("capabilities")
    entries = {entry["name"]: entry for entry in doc["data"]["commands"]}

    assert code == 0
    assert "mem summarise" in entries
    assert entries["mem summarise"]["when"]
    assert entries["mem summarise"]["example"].startswith("carto mem summarise")
    # A namespace is listed as its leaves; `mem` alone runs nothing.
    assert "mem" not in entries


def test_the_catalogue_offers_the_opt_out():
    """T07 leaves "does spending the user's quota need a visible opt-out" open.
    It does, and an opt-out an agent cannot discover is not one."""
    code, doc = _run_cli("capabilities", "--command", "mem summarise")
    flags = {flag["flag"] for flag in doc["data"]["commands"][0]["flags"]}

    assert code == 0
    assert "--no-host-agent" in flags


def test_cli_structural_path_end_to_end(repo):
    _capture(repo, "s1", "First real prompt here", "Second real prompt here")

    code, doc = _run_cli("mem", "summarise", "--repo", str(repo),
                         "--session", "s1", "--no-host-agent")

    assert code == 0
    assert doc["tool"] == "mem summarise"
    assert doc["data"]["summary_source"] == "structural"
    assert doc["data"]["observation"]["doc_type"] == "sessions"
    # One observation per response, so nothing pages.
    assert doc.get("page") is None


def test_cli_refuses_without_a_store(repo):
    code, doc = _run_cli("mem", "summarise", "--repo", str(repo))

    assert code == 2
    assert doc["error"]["remediation"]


def test_the_summary_comes_back_out_of_mem_search(repo):
    _capture(repo, "s1", "Chose offset paging over keyset for the cursor",
             "Abandoned the keyset attempt because the sort key was not unique")
    summarise.summarise(repo, session="s1", use_host=False)

    code, doc = _run_cli("mem", "search", "--repo", str(repo),
                         "--query", "keyset paging", "--doc-type", "sessions",
                         "--limit", "5")

    assert code == 0
    assert doc["search_mode"]
    assert [item["doc_type"] for item in doc["data"]["items"]] == ["sessions"]
    assert doc["data"]["items"][0]["summary_source"] == "structural"


def test_prompts_and_sessions_stay_separately_searchable(repo):
    _capture(repo, "s1", "Chose offset paging over keyset for the cursor",
             "Abandoned the keyset attempt because the sort key was not unique")
    summarise.summarise(repo, session="s1", use_host=False)

    _, prompts = _run_cli("mem", "search", "--repo", str(repo),
                          "--query", "keyset", "--doc-type", "prompts", "--limit", "5")
    _, sessions = _run_cli("mem", "search", "--repo", str(repo),
                           "--query", "keyset", "--doc-type", "sessions", "--limit", "5")

    assert len(prompts["data"]["items"]) == 2
    assert len(sessions["data"]["items"]) == 1
