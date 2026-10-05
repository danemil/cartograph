"""A graph an older parser built is rebuilt by the next update, and flagged.

0.9.3 fixed ``import x as y``, yet a graph 0.9.2 built kept the old parse:
``update`` re-parses only changed files, and with none changed it re-parsed
nothing. Only a manual ``carto build`` fixed it, and ``carto status`` said the
graph was current. The graph now records the parser that built it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from cartograph.graph import GraphStore
from cartograph.incremental import (
    PARSER_VERSION,
    PARSER_VERSION_METADATA_KEY,
    full_build,
    incremental_update,
)

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


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "helpers.py").write_text("def compute(x):\n    return x * 2\n")
    (root / "tests" / "test_helpers.py").write_text(
        "import helpers as h\n\n\ndef test_compute():\n    assert h.compute(2) == 4\n"
    )
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _age_graph(repo: Path) -> None:
    """Make the graph look like one an older release built.

    Drops the recorded parser version, as every graph before 0.9.4 lacks it,
    and the IMPORTS_FROM edges that only the newer parser emits for
    ``import x as y`` — the 0.9.2 shape.
    """
    conn = sqlite3.connect(repo / ".cartograph" / "graph.db")
    try:
        conn.execute("DELETE FROM metadata WHERE key = ?", (PARSER_VERSION_METADATA_KEY,))
        conn.execute("DELETE FROM edges WHERE kind = 'IMPORTS_FROM'")
        conn.commit()
    finally:
        conn.close()


def _imports(repo: Path) -> int:
    conn = sqlite3.connect(repo / ".cartograph" / "graph.db")
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM edges WHERE kind = 'IMPORTS_FROM'"
        ).fetchone()[0]
    finally:
        conn.close()


def _status_json(repo: Path) -> dict:
    proc = _carto("status", "--repo", str(repo), "--format", "json", cwd=repo)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_full_build_records_the_parser_version(repo):
    store = GraphStore(repo / "graph.db")
    try:
        full_build(repo, store)
        assert store.get_metadata(PARSER_VERSION_METADATA_KEY) == PARSER_VERSION
    finally:
        store.close()


def test_update_after_an_upgrade_rebuilds_in_full_and_says_so(repo):
    assert _carto("build", "--repo", str(repo), cwd=repo).returncode == 0
    assert _imports(repo) == 1
    _age_graph(repo)

    proc = _carto("update", "--repo", str(repo), "--format", "json", cwd=repo)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)["data"]

    # The old parse is gone even though no file changed.
    assert _imports(repo) == 1
    assert data["build_type"] == "full"
    assert data["rebuild_reason"] == "parser_version"
    assert data["files_parsed"] == 2
    assert "older Cartograph parser" in data["summary"]
    assert _status_json(repo)["data"]["stale"] is False

    # Once only: the next update has nothing to do.
    again = _carto("update", "--repo", str(repo), "--format", "json", cwd=repo)
    again_data = json.loads(again.stdout)["data"]
    assert again_data["build_type"] == "incremental"
    assert again_data["files_updated"] == 0
    assert "rebuild_reason" not in again_data


def test_update_text_names_the_reason_for_the_full_rebuild(repo):
    _carto("build", "--repo", str(repo), cwd=repo)
    _age_graph(repo)
    proc = _carto("update", "--repo", str(repo), cwd=repo)
    assert proc.returncode == 0, proc.stderr
    assert "Full rebuild (graph built by an older Cartograph parser)" in proc.stdout


def test_status_flags_a_graph_an_older_parser_built(repo):
    _carto("build", "--repo", str(repo), cwd=repo)
    fresh = _status_json(repo)["data"]
    assert fresh["stale"] is False
    assert fresh["stale_reason"] is None
    assert fresh["remediation"] is None

    _age_graph(repo)
    doc = _status_json(repo)
    assert doc["ok"] is True
    assert doc["data"]["stale"] is True
    assert doc["data"]["stale_reason"] == "built by an older Cartograph parser"
    assert doc["data"]["remediation"] == "carto build"

    text = _carto("status", "--repo", str(repo), cwd=repo).stdout
    assert "built by an older Cartograph parser" in text
    assert "carto build" in text

    # status reads the version; it never writes it.
    conn = sqlite3.connect(repo / ".cartograph" / "graph.db")
    try:
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = ?", (PARSER_VERSION_METADATA_KEY,)
        ).fetchone()
    finally:
        conn.close()
    assert row is None


def test_a_different_recorded_version_also_rebuilds(repo):
    store = GraphStore(repo / "graph.db")
    try:
        full_build(repo, store)
        store.set_metadata(PARSER_VERSION_METADATA_KEY, "0")
        result = incremental_update(repo, store, changed_files=[])
        assert result["full_rebuild"] is True
        assert result["rebuild_reason"] == "parser_version"
        assert result["files_updated"] == 2
        assert store.get_metadata(PARSER_VERSION_METADATA_KEY) == PARSER_VERSION
        # Not a C++ graph: the C++ identity flag stays out of it.
        assert "identity_rebuild" not in result
    finally:
        store.close()


def test_a_parse_error_does_not_make_every_update_a_rebuild(repo, monkeypatch):
    """A file that fails to parse must not hold the version back forever."""
    from cartograph.incremental import CodeParser

    original = CodeParser.parse_bytes

    def failing(parser, path, source):
        if Path(path).name == "helpers.py":
            raise RuntimeError("simulated parse failure")
        return original(parser, path, source)

    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    store = GraphStore(repo / "graph.db")
    try:
        full_build(repo, store)
        store.set_metadata(PARSER_VERSION_METADATA_KEY, "0")
        monkeypatch.setattr(CodeParser, "parse_bytes", failing)
        rebuilt = incremental_update(repo, store, changed_files=[])
        assert rebuilt["full_rebuild"] is True
        assert rebuilt["errors"]
        assert store.get_metadata(PARSER_VERSION_METADATA_KEY) == PARSER_VERSION
        assert "full_rebuild" not in incremental_update(repo, store, changed_files=[])
    finally:
        store.close()


def test_an_empty_graph_is_not_rebuilt_and_records_the_version(repo):
    store = GraphStore(repo / "graph.db")
    try:
        result = incremental_update(repo, store, changed_files=["helpers.py"])
        assert "full_rebuild" not in result
        assert result["files_updated"] == 1
        assert store.get_metadata(PARSER_VERSION_METADATA_KEY) == PARSER_VERSION
    finally:
        store.close()
