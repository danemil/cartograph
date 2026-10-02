"""A graph keeps working when its checkout is moved or mounted elsewhere.

The graph stores absolute paths. A user's A/B copied a repository, with its
``.cartograph/``, from one path to another; ``coverage`` then compared the
stored paths with the working tree at the new path, found none of them, and
said "searched 0 of 71 code files" over results that were complete. Every
answer warned the user, wrongly, that the ranking was partial. A Dev Container
that mounts the same checkout at ``/workspaces/<name>`` is the same case.

Reads compare repo-relative paths, against whichever root the stored paths are
under (``repo_paths.register_anchor``); ``update`` and ``build`` move the
stored paths to the root they run at, so later writes match. Each test runs
against a real ``git init`` repository copied with ``shutil.copytree``, the
original removed unless the test says otherwise.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from cartograph import cli
from cartograph.graph import GraphStore
from cartograph.incremental import get_db_path
from cartograph.tools.build import build_or_update_graph


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
        stdin=subprocess.DEVNULL,
    ).stdout


def _func(name: str, body_lines: int = 1, call: str = "") -> str:
    body = "".join(f"    x{i} = {i}\n" for i in range(body_lines))
    tail = f"    return {call}()\n" if call else "    return 0\n"
    return f"def {name}():\n{body}{tail}\n\n"


def _make_repo(root: Path) -> Path:
    (root / "pkg").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "pkg" / "a.py").write_text(_func("compute", 5) + _func("caller_a", 1, "compute"))
    (root / "pkg" / "b.py").write_text(
        "from pkg.a import compute\n\n\n" + _func("compute", 2) + _func("caller_b", 3, "compute")
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    build_or_update_graph(full_rebuild=True, repo_root=str(root), postprocess="none")
    return root


@pytest.fixture()
def moved(tmp_path: Path) -> Path:
    """A built repository copied to a new path, the original gone."""
    original = _make_repo(tmp_path / "original")
    copy = tmp_path / "elsewhere" / "copy"
    copy.parent.mkdir()
    shutil.copytree(original, copy, symlinks=True)
    shutil.rmtree(original)
    return copy


@pytest.fixture()
def mounted(tmp_path: Path) -> tuple[Path, Path]:
    """The same graph at two paths, both present: a Dev Container mount."""
    original = _make_repo(tmp_path / "host")
    copy = tmp_path / "workspaces" / "host"
    copy.parent.mkdir()
    shutil.copytree(original, copy, symlinks=True)
    return original, copy


def _run(argv, capsys, code: int = 0):
    with patch.object(sys, "argv", ["cartograph", *argv]):
        with pytest.raises(SystemExit) as exc:
            cli.main()
    out = capsys.readouterr().out
    assert exc.value.code == code, out
    return json.loads(out)


def _stored_files(root: Path) -> list[str]:
    store = GraphStore(get_db_path(root))
    try:
        return store.get_file_marker_paths()
    finally:
        store.close()


def test_coverage_is_complete_after_a_move(moved, capsys):
    env = _run(["large-functions", "--limit", "10", "--repo", str(moved)], capsys)
    assert env["data"]["coverage"] == "searched all 2 code files"
    assert "not covered" not in env["data"]["summary"]


def test_coverage_is_complete_where_both_paths_exist(mounted, capsys):
    _, copy = mounted
    env = _run(["search", "compute", "--repo", str(copy)], capsys)
    assert env["data"]["coverage"] == "searched all 2 code files"


def test_rows_are_repo_relative_after_a_move(moved, capsys):
    env = _run(["large-functions", "--limit", "10", "--repo", str(moved)], capsys)
    text = json.dumps(env["data"])
    assert "original" not in text, text
    assert env["data"]["results"][0] == "7 lines | Function | compute | pkg/a.py:1"


def test_a_relative_target_resolves_exactly_after_a_move(moved, capsys):
    """Two functions share the name; the qualified target picks one."""
    env = _run(
        ["query", "callers_of", "pkg/a.py::compute", "--repo", str(moved)], capsys,
    )
    assert "disambiguation" not in env["data"], env["data"]
    callers = env["data"]["results"]
    assert any("caller_a" in row for row in callers), callers


def test_importers_of_a_file_after_a_move(moved, capsys):
    env = _run(["query", "importers_of", "pkg/a.py", "--repo", str(moved)], capsys)
    assert env["data"]["results"], env["data"]


def test_architecture_layout_counts_the_graph_after_a_move(moved, capsys):
    env = _run(["architecture", "--repo", str(moved)], capsys)
    assert env["data"]["layout"]["graph_files"] == 2


def test_update_works_after_a_move(moved, capsys):
    (moved / "pkg" / "a.py").write_text(_func("compute", 40) + _func("caller_a", 1, "compute"))
    result = build_or_update_graph(
        full_rebuild=False, repo_root=str(moved), postprocess="none",
    )
    assert result.get("status") != "error", result
    prefix = moved.resolve().as_posix() + "/"
    assert all(p.startswith(prefix) for p in _stored_files(moved))
    env = _run(["large-functions", "--limit", "1", "--repo", str(moved)], capsys)
    assert env["data"]["results"] == ["42 lines | Function | compute | pkg/a.py:1"]
    assert env["data"]["coverage"] == "searched all 2 code files"


def test_update_keeps_unchanged_files_and_their_edges(moved, capsys):
    build_or_update_graph(full_rebuild=False, repo_root=str(moved), postprocess="none")
    env = _run(
        ["query", "callers_of", "pkg/b.py::compute", "--repo", str(moved)], capsys,
    )
    assert any("caller_b" in row for row in env["data"]["results"]), env["data"]


def test_impact_after_a_move_is_relative(moved, capsys):
    env = _run(["impact", "--files", "pkg/a.py", "--repo", str(moved)], capsys)
    assert env["data"]["totals"]["changed_nodes"] > 0
    assert "original" not in json.dumps(env["data"])


def test_detect_changes_sees_an_edit_after_a_move(moved, capsys):
    (moved / "pkg" / "a.py").write_text(
        _func("compute", 6) + _func("caller_a", 1, "compute")
    )
    env = _run(["detect-changes", "--repo", str(moved), "--format", "json"], capsys)
    assert env["data"].get("changed_functions"), env["data"]


def test_both_mounts_keep_answering_after_either_updates(mounted, capsys):
    """Host and container share one `.cartograph/`; an update at one path
    must not leave the other with a partial answer."""
    host, container = mounted
    # The container's update moves the stored paths to its root ...
    build_or_update_graph(full_rebuild=False, repo_root=str(container), postprocess="none")
    shutil.copy2(get_db_path(container), get_db_path(host))
    # ... and the host, reading that graph, still covers every file.
    env = _run(["large-functions", "--repo", str(host)], capsys)
    assert env["data"]["coverage"] == "searched all 2 code files"
    assert "workspaces" not in json.dumps(env["data"])


def test_a_graph_of_another_repository_is_not_adopted(tmp_path, capsys):
    """Sharing a file name or two does not make a graph this tree's."""
    other = _make_repo(tmp_path / "other")
    here = tmp_path / "here"
    (here / "pkg").mkdir(parents=True)
    _git(here, "init", "-q")
    (here / "pkg" / "a.py").write_text(_func("unrelated"))
    for i in range(3):
        (other / f"extra{i}.py").write_text(_func(f"e{i}"))
    build_or_update_graph(full_rebuild=True, repo_root=str(other), postprocess="none")
    (here / ".cartograph").mkdir()
    shutil.copy2(get_db_path(other), get_db_path(here))
    with pytest.raises(RuntimeError, match="different repository root"):
        from cartograph.incremental import incremental_update

        store = GraphStore(get_db_path(here))
        try:
            incremental_update(here, store, base="HEAD")
        finally:
            store.close()


def test_review_context_sees_an_edit_after_a_move(moved, capsys):
    (moved / "pkg" / "a.py").write_text(
        _func("compute", 6) + _func("caller_a", 1, "compute")
    )
    env = _run(["review-context", "--repo", str(moved)], capsys)
    assert env["data"]["facets"]["changed_nodes"]["total"] == 3, env["data"]
    assert "original" not in json.dumps(env["data"])



def test_flow_source_is_read_from_this_checkout(tmp_path, capsys):
    original = tmp_path / "original"
    _make_repo(original)
    build_or_update_graph(full_rebuild=True, repo_root=str(original), postprocess="full")
    copy = tmp_path / "copy"
    shutil.copytree(original, copy, symlinks=True)
    shutil.rmtree(original)
    flows = _run(["flows", "--repo", str(copy), "--detail", "full"], capsys)["data"]["flows"]
    assert flows, "the fixture has a call chain, so it has a flow"
    env = _run(
        ["flow", "--id", str(flows[0]["id"]), "--source", "--repo", str(copy)], capsys,
    )
    steps = env["data"]["flow"]["steps"]
    assert any(step.get("source") for step in steps), steps
