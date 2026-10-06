"""A test file is judged by its path inside the repository.

In a Dev Container the repository is checked out at ``/workspaces/dc-test``.
The test-file patterns were matched against the absolute path, so ``tests?/``
matched ``dc-test/`` and every file of the checkout was a test file:
``tests_for add_node`` then reported tests of a production function that
merely calls ``add_node`` as tests of ``add_node``. The same held for a
checkout under ``latest/``, ``tests/``, or any directory whose name ends in
``test``; and ``test_.*\\.py`` matched ``latest_x.py`` (or any ``test_*``
directory above the repository, which pytest's own ``tmp_path`` is).
"""

import sqlite3
import subprocess
from pathlib import Path

import pytest

from cartograph.parser import _is_test_file
from cartograph.tools.build import build_or_update_graph
from cartograph.tools.query import query_graph

CHECKOUT_DIRS = ["dc-test", "latest", "tests"]


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _build(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    build_or_update_graph(full_rebuild=True, repo_root=str(repo))


def _test_files(repo: Path) -> set[str]:
    conn = sqlite3.connect(repo / ".cartograph" / "graph.db")
    try:
        rows = conn.execute(
            "SELECT file_path FROM nodes WHERE kind = 'File' AND is_test = 1"
        ).fetchall()
    finally:
        conn.close()
    return {Path(r[0]).relative_to(repo.resolve()).as_posix() for r in rows}


def _python_repo(repo: Path) -> None:
    _write(repo, "pkg/store.py", "def add_node(conn, nd):\n    return nd\n")
    _write(repo, "pkg/ingest.py", (
        "import store\n\n"
        "def build(conn):\n    store.add_node(conn, {})\n"
    ))
    _write(repo, "pkg/latest_report.py", (
        "import store\n\n"
        "def report(conn):\n    store.add_node(conn, {})\n"
    ))
    _write(repo, "pkg/tests/test_store.py", (
        "import store\n\n"
        "def test_add():\n    store.add_node(None, {})\n"
    ))
    _write(repo, "pkg/tests/test_ingest.py", (
        "import ingest\n\n"
        "def test_build():\n    ingest.build(None)\n"
    ))
    _write(repo, "pkg/tests/test_query.py", (
        "import store\n\n"
        "def _fixture():\n    store.add_node(None, {})\n\n"
        "def test_query():\n    _fixture()\n"
    ))


@pytest.mark.parametrize("checkout", CHECKOUT_DIRS)
def test_only_real_test_files_are_marked(tmp_path: Path, checkout: str):
    repo = tmp_path / checkout
    _python_repo(repo)
    _build(repo)
    assert _test_files(repo) == {
        "pkg/tests/test_store.py",
        "pkg/tests/test_ingest.py",
        "pkg/tests/test_query.py",
    }


@pytest.mark.parametrize("checkout", CHECKOUT_DIRS)
def test_tests_for_under_a_test_named_checkout(tmp_path: Path, checkout: str):
    repo = tmp_path / checkout
    _python_repo(repo)
    _build(repo)
    result = query_graph("tests_for", "pkg/store.py::add_node", repo_root=str(repo))
    found = {(r["name"], r.get("via", "").rsplit("::", 1)[-1])
             for r in result["results"]}
    # Directly, and through a helper defined in a real test file. Not through
    # ingest.build or latest_report.report: those are production code.
    assert found == {("test_add", ""), ("test_query", "_fixture")}


def test_flows_under_a_test_named_checkout(tmp_path: Path):
    def make(repo: Path) -> None:
        _write(repo, "src/app.js", (
            "function compute() { return 'x'; }\n"
            "function main(s) { return /x/.test(compute()); }\n"
            "module.exports = { main };\n"
        ))

    from cartograph.flows import trace_flows
    from cartograph.graph import GraphStore

    flows = {}
    for name in ("plain", "dc-test"):
        repo = tmp_path / name
        make(repo)
        _build(repo)
        store = GraphStore(repo / ".cartograph" / "graph.db")
        try:
            flows[name] = sorted(
                (f["name"], f["node_count"]) for f in trace_flows(store)
            )
            tests = store._conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE kind = 'Test'"
            ).fetchone()[0]
        finally:
            store.close()
        # `/x/.test(...)` is a regex method in production code, not a test.
        assert tests == 0, name
    assert flows["dc-test"], "no flows for a checkout under dc-test/"
    assert flows["dc-test"] == flows["plain"]


@pytest.mark.parametrize("root", ["/workspaces/dc-test", "/src/latest", "/x/tests", None])
def test_classifier_uses_the_repo_relative_path(root):
    def is_test(rel: str) -> bool:
        if root is None:
            return _is_test_file(rel)
        return _is_test_file(f"{root}/{rel}", root)

    # Production files, whatever the checkout is called.
    for rel in ("pkg/ingest.py", "latest_report.py", "src/contest/app.ts",
                "latest/x.go", "attest/y.py", "pytest_plugin/z.py"):
        assert not is_test(rel), rel
    # Filename conventions.
    for rel in ("test_x.py", "pkg/test_x.py", "pkg/x_test.py", "x_test.go",
                "src/FooTest.java", "src/FooTest.kt", "a/x.spec.ts",
                "a/x.test.js", "a/x.test.tsx", "lib/x_test.dart",
                "R/test-util.R", "src/x_test.res"):
        assert is_test(rel), rel
    # Directory conventions, anchored to a path component.
    for rel in ("tests/helpers.py", "pkg/tests/conftest.py", "test/foo.py",
                "src/__tests__/User.ts", "__tests__/User.ts",
                "tests/testthat/test-a.R", "test/runtests.jl", "test/util.jl",
                "e2e-tests/login.py", "integration_tests/db.py"):
        assert is_test(rel), rel


def test_classifier_handles_backslashes_and_paths_outside_root():
    assert _is_test_file("C:\\work\\dc-test\\src\\__tests__\\U.ts", "C:\\work\\dc-test")
    assert not _is_test_file("C:\\work\\dc-test\\src\\app.ts", "C:\\work\\dc-test")
    # A path outside the root is classified as given.
    assert _is_test_file("/elsewhere/tests/a.py", "/workspaces/dc-test")
