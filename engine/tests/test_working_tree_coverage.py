"""The graph covers the working tree, top-N means top N, and coverage is said.

A user-run A/B (Copilot CLI, gpt-5.4-mini, five rounds) asked for the ten
largest functions. With carto, 0/5 answers were right, against 2/5 without:

- ``large-functions --limit 10`` kept a hidden ``--min-lines 50``, so a small
  repository returned 6 rows and the agent reported 6 as the top 10;
- the graph was built from ``git ls-files`` alone, so functions in an
  untracked, not-ignored skill directory were invisible, and nothing in the
  response said any file had been left out.

Each test here runs against a real ``git init`` repository.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from cartograph import cli
from cartograph.graph import GraphStore
from cartograph.incremental import (
    full_build,
    get_changed_files,
    get_db_path,
    repository_files,
)
from cartograph.tools.build import build_or_update_graph


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
        stdin=subprocess.DEVNULL,
    ).stdout


def _func(name: str, body_lines: int = 1) -> str:
    body = "".join(f"    x{i} = {i}\n" for i in range(body_lines))
    return f"def {name}():\n{body}    return 0\n\n"


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "tracked.py").write_text(_func("tracked_fn"))
    (root / ".gitignore").write_text("ignored/\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _function_names(root: Path) -> set[str]:
    store = GraphStore(get_db_path(root))
    try:
        rows = store._conn.execute(
            "SELECT name FROM nodes WHERE kind = 'Function'"
        ).fetchall()
        return {r["name"] for r in rows}
    finally:
        store.close()


def _build(root: Path) -> dict:
    return build_or_update_graph(full_rebuild=True, repo_root=str(root), postprocess="none")


def _update(root: Path) -> dict:
    # The path `carto update` and the Stop hook take: no base, so it is
    # resolved to the commit the graph was last built at.
    return build_or_update_graph(full_rebuild=False, repo_root=str(root), postprocess="none")


# --------------------------------------------------------------------------
# Working-tree coverage
# --------------------------------------------------------------------------


def test_untracked_not_ignored_file_is_built(repo):
    skill = repo / ".github" / "skills" / "deck" / "scripts"
    skill.mkdir(parents=True)
    (skill / "render.py").write_text(_func("render_deck"))
    _build(repo)
    assert {"tracked_fn", "render_deck"} <= _function_names(repo)


def test_ignored_file_is_not_built(repo):
    (repo / "ignored").mkdir()
    (repo / "ignored" / "secret.py").write_text(_func("ignored_fn"))
    _build(repo)
    names = _function_names(repo)
    assert "ignored_fn" not in names
    assert "tracked_fn" in names


def test_repository_files_lists_untracked_but_not_ignored(repo):
    (repo / "new.py").write_text(_func("new_fn"))
    (repo / "ignored").mkdir()
    (repo / "ignored" / "x.py").write_text(_func("x"))
    files, listed_by_vcs = repository_files(repo)
    assert listed_by_vcs
    assert "new.py" in files
    assert "ignored/x.py" not in files


def test_update_picks_up_a_new_untracked_file(repo):
    _build(repo)
    (repo / "later.py").write_text(_func("later_fn"))
    result = _update(repo)
    assert "later_fn" in _function_names(repo)
    assert result["files_updated"] >= 1


def test_update_reparses_an_edited_untracked_file(repo):
    (repo / "scratch.py").write_text(_func("first_fn"))
    _build(repo)
    (repo / "scratch.py").write_text(_func("second_fn"))
    _update(repo)
    names = _function_names(repo)
    assert "second_fn" in names
    assert "first_fn" not in names


def test_unchanged_untracked_file_is_not_reparsed(repo):
    (repo / "scratch.py").write_text(_func("scratch_fn"))
    _build(repo)
    result = _update(repo)
    assert result["files_updated"] == 0
    # Not even queued: a changed file also queues its dependents for
    # re-parsing, which a long-lived untracked directory would pay every time.
    assert "scratch.py" not in result["changed_files"]


def test_update_removes_a_deleted_untracked_file(repo):
    (repo / "scratch.py").write_text(_func("scratch_fn"))
    _build(repo)
    assert "scratch_fn" in _function_names(repo)
    (repo / "scratch.py").unlink()
    _update(repo)
    assert "scratch_fn" not in _function_names(repo)


def test_update_removes_an_untracked_file_once_it_is_ignored(repo):
    (repo / "scratch.py").write_text(_func("scratch_fn"))
    _build(repo)
    (repo / ".gitignore").write_text("ignored/\nscratch.py\n")
    _update(repo)
    assert "scratch_fn" not in _function_names(repo)
    assert "tracked_fn" in _function_names(repo)


def test_changed_files_include_untracked_for_detect_changes(repo):
    (repo / "tracked.py").write_text(_func("tracked_fn") + _func("more"))
    (repo / "new.py").write_text(_func("new_fn"))
    (repo / "ignored").mkdir()
    (repo / "ignored" / "x.py").write_text(_func("x"))
    changed = get_changed_files(repo, "HEAD")
    assert "tracked.py" in changed
    assert "new.py" in changed
    assert "ignored/x.py" not in changed


def test_full_build_with_submodule_recursion_still_lists_untracked(repo):
    (repo / "new.py").write_text(_func("new_fn"))
    store = GraphStore(get_db_path(repo))
    try:
        full_build(repo, store, recurse_submodules=True)
    finally:
        store.close()
    assert "new_fn" in _function_names(repo)


# --------------------------------------------------------------------------
# Top-N means top N
# --------------------------------------------------------------------------


def _run(argv, capsys):
    with patch.object(sys, "argv", ["cartograph", *argv]):
        with pytest.raises(SystemExit) as exc:
            cli.main()
    assert exc.value.code == 0
    return json.loads(capsys.readouterr().out)


@pytest.fixture()
def twelve_small(repo):
    # Twelve functions of 3..14 lines: all under the old hidden 50-line floor.
    (repo / "many.py").write_text("".join(_func(f"f{i:02}", i + 1) for i in range(12)))
    _git(repo, "add", "many.py")
    _git(repo, "commit", "-q", "-m", "many")
    _build(repo)
    return repo


def test_limit_without_min_lines_returns_the_top_n(twelve_small, capsys):
    env = _run(["large-functions", "--limit", "10", "--repo", str(twelve_small)], capsys)
    rows = env["data"]["results"]
    assert len(rows) == 10
    assert rows[0].split(" | ")[2] == "f11"
    assert env["page"]["has_more"] is True
    summary = env["data"]["summary"]
    assert summary.startswith("Top 10 of 13 functions by line count")
    assert "no --min-lines" in summary


def test_default_is_top_twenty_with_no_threshold(twelve_small, capsys):
    env = _run(["large-functions", "--repo", str(twelve_small)], capsys)
    # 12 in many.py, 1 in tracked.py: all of them, since there are under 20.
    assert len(env["data"]["results"]) == 13
    assert env["page"]["limit"] == 20
    assert env["page"]["has_more"] is False


def test_explicit_min_lines_still_filters(twelve_small, capsys):
    env = _run(
        ["large-functions", "--min-lines", "10", "--limit", "10",
         "--repo", str(twelve_small)],
        capsys,
    )
    # f07..f11 are 10..14 lines (def + body + return).
    assert len(env["data"]["results"]) == 5
    assert env["data"]["summary"].startswith("5 functions >= 10 lines")


# --------------------------------------------------------------------------
# Coverage is stated
# --------------------------------------------------------------------------


@pytest.fixture()
def mixed(repo):
    """Code the graph parses, and code it cannot or does not, by reason."""
    (repo / "a.py").write_text(_func("a_fn"))
    (repo / "run.bat").write_text("@echo off\n")              # no parser
    (repo / "page.html").write_text("<p>hi</p>\n")            # no parser
    (repo / "dist").mkdir()
    (repo / "dist" / "bundle.js").write_text("function b(){}\n")  # generated
    (repo / "skipme").mkdir()
    (repo / "skipme" / "s.py").write_text(_func("s_fn"))      # .cartographignore
    (repo / ".cartographignore").write_text("skipme/\n")
    (repo / "README.md").write_text("# docs are not code\n")  # not counted
    _build(repo)
    (repo / "late.py").write_text(_func("late_fn"))           # after the build
    return repo


def test_coverage_counts_by_reason(mixed, capsys):
    env = _run(["large-functions", "--limit", "10", "--repo", str(mixed)], capsys)
    coverage = env["data"]["coverage"]
    # Parsed: tracked.py, a.py. Code files: those, run.bat, page.html,
    # dist/bundle.js, skipme/s.py, late.py.
    assert coverage.startswith("searched 2 of 7 code files; not covered: ")
    assert "2 no parser (.bat, .html)" in coverage
    assert "1 generated or vendored" in coverage
    assert "1 excluded by ignore rules" in coverage
    assert "1 not in the graph (run carto update)" in coverage
    # The summary, which an agent may read alone, says it is partial.
    assert "5 of 7 code files not covered" in env["data"]["summary"]


def test_coverage_is_complete_when_everything_is_parsed(repo, capsys):
    _build(repo)
    env = _run(["large-functions", "--repo", str(repo)], capsys)
    assert env["data"]["coverage"] == "searched all 1 code files"
    assert "not covered" not in env["data"]["summary"]


def test_coverage_caps_examples_at_three(repo, capsys):
    for ext in ("bat", "cmd", "html", "css", "scss"):
        (repo / f"f.{ext}").write_text("x\n")
    _build(repo)
    env = _run(["large-functions", "--repo", str(repo)], capsys)
    coverage = env["data"]["coverage"]
    assert "5 no parser (" in coverage
    listed = coverage.split("5 no parser (")[1].split(")")[0]
    assert len(listed.split(", ")) == 3


@pytest.mark.parametrize(
    "argv",
    [
        ["search", "a_fn", "--limit", "5"],
        ["query", "children_of", "a.py"],
        ["dead-code", "--format", "json"],
        ["refactor", "rename", "--old-name", "a_fn", "--new-name", "b_fn"],
    ],
)
def test_other_list_commands_state_coverage(mixed, capsys, argv):
    env = _run([*argv, "--repo", str(mixed)], capsys)
    assert env["data"]["coverage"].startswith("searched 2 of 7 code files")
