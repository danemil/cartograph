"""What build and update say must agree with the numbers beside it.

Two contradictions an agent read in 0.9.3:

- ``update`` on an up-to-date graph after a commit that touched only
  ``.gitignore`` said ``"files_updated": 0`` and ``"changed_files":
  [".gitignore"]``. ``changed_files`` now lists only what the update applied
  to the graph; a changed file it skipped is under ``ignored_changes``.
- ``build`` said "created 450 nodes and 5314 edges" beside ``total_edges:
  5222``: the summary was written before post-processing, the totals after.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]


def _carto(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(ENGINE)}
    return subprocess.run(
        [sys.executable, "-m", "cartograph", *args],
        cwd=cwd, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
        timeout=120,
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   stdin=subprocess.DEVNULL)


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "a.py").write_text("def f():\n    return 1\n")
    (root / "b.py").write_text("def g():\n    return 2\n")
    (root / ".gitignore").write_text("*.log\n")
    _commit(root, "init")
    assert _carto("build", "--repo", str(root), cwd=root).returncode == 0
    return root


def _update(repo: Path) -> dict:
    proc = _carto("update", "--repo", str(repo), "--format", "json", cwd=repo)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["data"]


def test_a_non_code_change_is_an_ignored_change_not_a_changed_file(repo):
    (repo / ".gitignore").write_text("*.log\n*.tmp\n")
    _commit(repo, "ignore tmp")

    data = _update(repo)

    assert data["files_updated"] == 0
    assert data["changed_files"] == []
    assert data["ignored_changes"] == [".gitignore"]


def test_changed_files_lists_what_was_reparsed_beside_what_was_skipped(repo):
    (repo / "a.py").write_text("def f():\n    return 10\n")
    (repo / "notes.md").write_text("# notes\n")
    (repo / ".gitignore").write_text("*.log\n*.tmp\n")
    _commit(repo, "mixed")

    data = _update(repo)

    assert data["files_updated"] == 1
    assert data["changed_files"] == ["a.py"]
    assert sorted(data["ignored_changes"]) == [".gitignore", "notes.md"]
    assert "notes.md" not in data["summary"]


def test_a_deleted_code_file_counts_as_a_change_applied(repo):
    (repo / "b.py").unlink()
    _commit(repo, "drop b")

    data = _update(repo)

    assert data["files_updated"] == 1
    assert data["changed_files"] == ["b.py"]
    assert data.get("ignored_changes", []) == []


def test_the_build_summary_quotes_the_final_totals(repo, monkeypatch):
    """Post-processing can drop edges; the summary must not predate it."""
    from cartograph.tools import build as build_mod

    original = build_mod._run_postprocess

    def drop_an_edge(store, *args, **kwargs):
        warnings = original(store, *args, **kwargs)
        store._conn.execute(
            "DELETE FROM edges WHERE id = (SELECT MIN(id) FROM edges)"
        )
        store.commit()
        return warnings

    monkeypatch.setattr(build_mod, "_run_postprocess", drop_an_edge)

    result = build_mod.build_or_update_graph(full_rebuild=True, repo_root=str(repo))

    assert (
        f"created {result['total_nodes']} nodes and {result['total_edges']} edges"
        in result["summary"]
    ), result["summary"]
