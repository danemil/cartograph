"""`import x as y` is an import: tests that call through the alias are tests.

The aliased form wraps the module in an ``aliased_import`` node. When the
parser skipped it, the file had no IMPORTS_FROM edge, so nothing proved which
``add_node`` a test's ``gs.add_node(...)`` called and ``tests_for`` found none.
"""

import subprocess
from pathlib import Path

from cartograph.parser import CodeParser
from cartograph.tools.build import build_or_update_graph
from cartograph.tools.query import query_graph


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_aliased_import_emits_an_import_edge(tmp_path: Path):
    source = tmp_path / "a.py"
    source.write_text("import pkg.store as st, other as o\nimport plain\n")
    _, edges = CodeParser().parse_file(source)
    imported = sorted(e.target for e in edges if e.kind == "IMPORTS_FROM")
    assert imported == ["other", "pkg.store", "plain"]


def test_tests_for_finds_a_test_calling_through_an_aliased_import(tmp_path: Path):
    _write(tmp_path, "pkg/store.py", "def add_node(conn, nd):\n    return nd\n")
    _write(tmp_path, "pkg/ingest.py", (
        "import store as gs\n\n"
        "def build(conn):\n    gs.add_node(conn, {})\n"
    ))
    _write(tmp_path, "pkg/tests/test_store.py", (
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n"
        "import store as gs\n\n"
        "def test_add():\n    gs.add_node(None, {})\n"
    ))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    build_or_update_graph(full_rebuild=True, repo_root=str(tmp_path))

    result = query_graph(
        "tests_for", "pkg/store.py::add_node", repo_root=str(tmp_path),
    )
    found = {(r["name"], r["file_path"].replace("\\", "/").split("pkg/", 1)[1])
             for r in result["results"]}
    # The test is found; the production caller in ingest.py is not a test.
    assert found == {("test_add", "tests/test_store.py")}


def test_tests_for_follows_a_test_file_helper_one_hop(tmp_path: Path):
    _write(tmp_path, "pkg/store.py", "def add_node(conn, nd):\n    return nd\n")
    _write(tmp_path, "pkg/tests/test_query.py", (
        "import store as gs\n\n"
        "def _fixture():\n    gs.add_node(None, {})\n\n"
        "def _unused_helper():\n    gs.add_node(None, {})\n\n"
        "def test_query():\n    _fixture()\n"
    ))
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    build_or_update_graph(full_rebuild=True, repo_root=str(tmp_path))

    full = query_graph(
        "tests_for", "pkg/store.py::add_node", repo_root=str(tmp_path),
    )
    [test] = full["results"]
    assert test["name"] == "test_query"
    assert test["indirect"] is True
    assert test["via"].endswith("::_fixture")

    from cartograph.compact import node_row
    assert node_row(test).endswith("| via _fixture")
