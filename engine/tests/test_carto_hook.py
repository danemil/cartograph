"""``carto hook`` — the host-invoked entry point, and the launcher behind it.

The pins that matter most here are negative ones. A hook that exits 2 because
the graph is missing is not a degraded hook, it is a harmful one: in the hook
protocol 2 means "blocking feedback to the model", so the host would halt the
session to tell the model about a precondition the model never depended on.
The query protocol's exit codes and its envelope must therefore stay out of
this path entirely, and the tests below say so directly rather than leaving it
to review.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from cartograph import capabilities, cli, hook, skills


# --- The two protocols must not be conflated -------------------------------


def test_missing_graph_exits_zero_not_precondition(tmp_path, capsys):
    """The pin. Exit 2 here would mean "stop, here is blocking feedback"."""
    exit_code = hook.run("session-status", repo=str(tmp_path))

    assert exit_code == 0
    assert exit_code != 2, "2 is blocking feedback in the hook protocol"
    captured = capsys.readouterr()
    assert "No graph yet" in captured.out
    with pytest.raises(json.JSONDecodeError):
        json.loads(captured.out)


def test_hook_module_never_imports_the_envelope():
    """No envelope helper may be reachable from the hook path.

    Routing a hook through ``envelope.emit`` is the mistake this module exists
    to prevent, and it would look entirely reasonable in a diff. Read from the
    syntax tree rather than the text, so the module can still explain itself in
    prose without tripping its own guard.
    """
    import ast

    tree = ast.parse(Path(hook.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported += [f"{node.module or ''}.{a.name}" for a in node.names]

    assert not [name for name in imported if "envelope" in name], (
        "hook.py imports the envelope — the hook protocol is not the query "
        "protocol, and exit 2 does not mean the same thing in both"
    )
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called.isdisjoint({"ok", "error", "emit", "fit"})


@pytest.mark.parametrize("event", ["session-status", "file-update", "NoSuchEvent"])
def test_every_event_exits_zero(event, tmp_path):
    assert hook.run(event, repo=str(tmp_path)) == 0


def test_unknown_event_complains_on_stderr_only(tmp_path, capsys):
    """stdout may reach the model; stderr is the host's log."""
    hook.run("PreCompact", repo=str(tmp_path))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "unknown event" in captured.err


def test_cli_dispatch_exits_zero_on_a_repo_with_no_graph(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["carto", "hook", "SessionStart", "--repo", str(tmp_path)])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith("[carto]")


# --- SessionStart: one bounded, prescriptive line --------------------------


#: Generous next to the ~140 the design budgets, because the point of the cap
#: is to catch a line growing into a paragraph, not to police a word.
_LINE_BUDGET = 160


@pytest.mark.parametrize("state", [hook.READY, hook.STALE, hook.ABSENT])
def test_session_line_is_one_bounded_line(state):
    line = hook._SESSION_LINES[state]

    assert "\n" not in line
    assert len(line) <= _LINE_BUDGET
    assert line.startswith("[carto] ")


@pytest.mark.parametrize("state", [hook.READY, hook.STALE, hook.ABSENT])
def test_session_line_points_rather_than_enumerates(state):
    """Listing commands is ``carto capabilities``' job, paid for once."""
    line = hook._SESSION_LINES[state]

    assert line.count("carto ") - line.count("[carto] ") <= 1


def test_session_start_emits_exactly_once(tmp_path, capsys):
    hook.run("session-status", repo=str(tmp_path))

    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_graph_state_reads_absent_stale_and_ready(tmp_path, monkeypatch):
    db_path = tmp_path / ".cartograph" / "graph.db"
    monkeypatch.setattr(hook, "_built_on_another_branch", lambda *_: False)
    assert hook.graph_state(tmp_path) == hook.ABSENT

    db_path.parent.mkdir(parents=True)
    db_path.write_bytes(b"")
    assert hook.graph_state(tmp_path) == hook.READY

    monkeypatch.setattr(hook, "_built_on_another_branch", lambda *_: True)
    assert hook.graph_state(tmp_path) == hook.STALE


def test_graph_state_does_not_create_the_data_directory(tmp_path):
    """A hook reporting on the graph must not be what brings one into being."""
    assert hook.graph_state(tmp_path) == hook.ABSENT
    assert not (tmp_path / ".cartograph").exists()


def test_stale_is_the_same_test_status_reports(tmp_path):
    import sqlite3

    db_path = tmp_path / "graph.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO metadata VALUES ('git_branch', 'main')")
    conn.commit()
    conn.close()

    import cartograph.incremental as incremental

    original = incremental._git_branch_info
    try:
        incremental._git_branch_info = lambda _root: ("main", "sha")
        assert hook._built_on_another_branch(tmp_path, db_path) is False
        incremental._git_branch_info = lambda _root: ("feature/x", "sha")
        assert hook._built_on_another_branch(tmp_path, db_path) is True
    finally:
        incremental._git_branch_info = original


# --- Detachment ------------------------------------------------------------


def test_posix_detach_starts_a_new_session():
    assert hook._detach_kwargs("linux") == {"start_new_session": True}
    assert hook._detach_kwargs("darwin") == {"start_new_session": True}


def test_windows_detach_uses_creation_flags_not_an_ampersand():
    """Upstream defect #5: ``&`` backgrounds nothing on Windows."""
    kwargs = hook._detach_kwargs("win32")

    assert "start_new_session" not in kwargs
    flags = kwargs["creationflags"]
    assert flags & 0x00000008, "DETACHED_PROCESS not set"
    assert flags & 0x00000200, "CREATE_NEW_PROCESS_GROUP not set"


def test_generated_hook_commands_do_not_background_with_an_ampersand():
    config = skills.generate_hooks_config(Path("/repo"))
    for entries in config["hooks"].values():
        for entry in entries:
            for inner in entry["hooks"]:
                assert not inner["command"].rstrip().endswith("&")


def test_spawn_detached_hands_the_child_no_stdio(monkeypatch):
    """A host waiting for EOF on the hook's stdout would wait for the child."""
    seen = {}

    def _fake_popen(argv, **kwargs):
        seen.update(kwargs)
        seen["argv"] = argv
        return object()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    assert hook.spawn_detached(["true"]) is True

    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["stdout"] is subprocess.DEVNULL
    assert seen["stderr"] is subprocess.DEVNULL
    assert seen["close_fds"] is True


def test_spawn_detached_is_silent_when_the_binary_is_gone(tmp_path):
    assert hook.spawn_detached([str(tmp_path / "definitely-not-a-binary")]) is False


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX orphan check")
def test_spawned_child_outlives_its_parent(tmp_path):
    """The whole point, exercised for real rather than through a mock.

    A parent that exits immediately would take an attached child down with it
    (or leave the host waiting on its stdio). The child here writes a file a
    second after the parent is gone.
    """
    marker = tmp_path / "survived"
    child = tmp_path / "child.py"
    child.write_text(
        textwrap.dedent(
            f"""
            import time
            time.sleep(1.5)
            open({str(marker)!r}, "w").write("ok")
            """
        ),
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(Path(hook.__file__).parent.parent)!r})
            from cartograph import hook
            hook.spawn_detached([{sys.executable!r}, {str(child)!r}])
            """
        ),
        encoding="utf-8",
    )

    subprocess.run([sys.executable, str(parent)], check=True, timeout=30)
    assert not marker.exists(), "parent did not return before the child finished"

    deadline = time.time() + 20
    while time.time() < deadline and not marker.exists():
        time.sleep(0.1)
    assert marker.exists(), "child died with its parent — it was not detached"


def test_file_update_launches_an_update_and_returns(tmp_path, monkeypatch):
    launched = {}
    monkeypatch.setattr(
        hook, "spawn_detached", lambda argv, **kw: launched.update(argv=argv, **kw) or True
    )

    assert hook.run("PostToolUse", repo=str(tmp_path)) == 0

    assert launched["argv"] == hook.update_argv(tmp_path)
    assert "update" in launched["argv"]
    assert launched["cwd"] == tmp_path


def test_update_argv_does_not_depend_on_path(tmp_path):
    """The child starts from this interpreter, not from whatever PATH holds."""
    argv = hook.update_argv(tmp_path)

    assert argv[:3] == [sys.executable, "-m", "cartograph"]


@pytest.mark.parametrize(
    "build", [hook.update_argv, lambda repo: hook.summarise_argv(repo, "s1")]
)
def test_a_frozen_build_reenters_its_own_binary(tmp_path, monkeypatch, build):
    """A PyInstaller ``carto`` has no ``-m``: it reads ``cartograph`` as a
    subcommand name and exits 1, behind DEVNULL, so the detached work never
    happens and nothing says so."""
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    argv = build(tmp_path)

    assert argv[0] == sys.executable
    assert "-m" not in argv
    assert argv[1] in {"update", "mem"}


# --- Re-entrancy -----------------------------------------------------------


def test_marker_suppresses_every_event(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(hook.REENTRY_MARKER, "1")
    monkeypatch.setattr(
        hook,
        "spawn_detached",
        lambda *a, **k: pytest.fail("a hook inside a hook started work"),
    )

    assert hook.run("session-status", repo=str(tmp_path)) == 0
    assert hook.run("file-update", repo=str(tmp_path)) == 0
    assert capsys.readouterr().out == ""


def test_spawn_detached_marks_the_child(monkeypatch):
    """The marker has to be inherited, or the guard only covers one level."""
    seen = {}
    monkeypatch.setattr(
        subprocess, "Popen", lambda argv, **kw: seen.update(kw) or object()
    )
    monkeypatch.delenv(hook.REENTRY_MARKER, raising=False)

    hook.spawn_detached(["true"])

    assert seen["env"][hook.REENTRY_MARKER] == "1"
    assert hook.REENTRY_MARKER not in os.environ, "the marker leaked into this process"


# --- Reachability ----------------------------------------------------------


def test_hook_is_not_agent_facing():
    assert "hook" in capabilities._NOT_AGENT_FACING


def test_capabilities_does_not_list_hook():
    parser = _build_parser()
    catalogue = capabilities.build_catalogue(parser)

    assert "hook" not in {c["name"] for c in catalogue["commands"]}


def _build_parser():
    """The live parser, obtained the only way ``main`` exposes it."""
    import argparse

    captured = {}
    original = argparse.ArgumentParser.parse_args

    def _capture(self, *args, **kwargs):
        captured.setdefault("parser", self)
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = _capture
    try:
        with pytest.raises(SystemExit):
            cli.main()
    finally:
        argparse.ArgumentParser.parse_args = original
    return captured["parser"]


@pytest.mark.parametrize(
    ("spelling", "resolved"),
    [
        ("SessionStart", "session-status"),
        ("sessionStart", "session-status"),
        ("session_start", "session-status"),
        ("startup", "session-status"),
        ("resume", "session-status"),
        ("PostToolUse", "file-update"),
        ("afterFileEdit", "file-update"),
        ("AfterTool", "file-update"),
        ("file-changed", "file-update"),
    ],
)
def test_host_spellings_resolve(spelling, resolved):
    assert hook.resolve_event(spelling) == resolved


# --- The generated host configs call the Python, not a shell one-liner -----


def test_generated_claude_hooks_call_carto_hook():
    config = skills.generate_hooks_config(Path("/repo"))

    session = config["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    post = config["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert "carto hook session-status" in session
    assert "carto hook file-update" in post


def test_git_guard_still_precedes_the_work():
    """#312, carried forward.

    In a workspace root with no ``.git`` the hook must no-op rather than error
    on every tool call, which it does by short-circuiting on the guard. The
    upstream test for this looks for the literal ``carto update`` / ``carto
    status`` the rewiring replaced; the property it protects is unchanged.
    """
    config = skills.generate_hooks_config(Path("/repo"))

    for event, work in (
        ("SessionStart", "carto hook session-status"),
        ("PostToolUse", "carto hook file-update"),
    ):
        cmd = config["hooks"][event][0]["hooks"][0]["command"]
        assert cmd.index("git rev-parse --git-dir") < cmd.index(work)


def test_generated_hooks_keep_the_path_guard_and_runtime_repo():
    """Both properties predate this work and both are load-bearing (#549, #558)."""
    config = skills.generate_hooks_config(Path("/home/someone/checkout"))

    for entries in config["hooks"].values():
        for entry in entries:
            for inner in entry["hooks"]:
                # The guard must name the binary the hook INVOKES. Guarding on the
                # long alias while running `carto` made every hook a silent
                # no-op wherever only `carto` was installed.
                assert "command -v carto >/dev/null 2>&1 || exit 0" in inner["command"]
                assert "git rev-parse --show-toplevel" in inner["command"]
                assert "/home/someone/checkout" not in inner["command"]


def test_generated_hooks_are_silent_when_the_binary_is_absent(tmp_path):
    """Run the real command line with an empty PATH: no output, exit 0."""
    config = skills.generate_hooks_config(tmp_path)
    command = config["hooks"]["SessionStart"][0]["hooks"][0]["command"]

    result = subprocess.run(
        ["/bin/sh", "-c", command],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        input="{}",
        env={"PATH": "/usr/bin:/bin"},
        timeout=30,
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_codebuddy_inherits_the_shared_command(tmp_path):
    settings = json.loads(
        skills.install_codebuddy_hooks(tmp_path).read_text(encoding="utf-8")
    )
    post = settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]

    assert "carto hook file-update" in post


def test_gemini_scripts_call_carto_hook(tmp_path):
    skills.install_gemini_cli_hooks(tmp_path)
    hooks_dir = tmp_path / ".gemini" / "hooks"

    session = (hooks_dir / "crg-session-start.sh").read_text(encoding="utf-8")
    update = (hooks_dir / "crg-update.sh").read_text(encoding="utf-8")
    assert "carto hook session-status" in session
    assert "carto hook file-update" in update


def test_uninstall_still_recognises_the_command_it_used_to_write():
    """Ownership is matched by exact string, so a rewrite orphans the old one."""
    from cartograph import uninstall

    superseded = uninstall._superseded_repo_hook_commands()

    assert any("carto update --skip-flows" in c for c in superseded)
    assert any("carto status" in c for c in superseded)
