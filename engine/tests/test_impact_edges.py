"""`impact` says how each dependent depends on the change.

report6, T5: asked "who calls add_node, and what would break if its
signature changed?", one answer of five read `impact --files graph_store.py`
and called every importer of the file a "directly impacted runtime path".
The rows said `direct | File | ingest_docs.py` for an importer exactly as
they said `direct | Function | ingest` for a caller. An import does not break
when a signature changes; a call does. These pin the rows and the summary
that keep the two apart, on a repository parsed for real.
"""

from __future__ import annotations

import json
import subprocess
import sys
from unittest.mock import patch

import pytest

from cartograph import cli
from cartograph import graph as graph_module

_FILES = {
    "kn/graph_store.py": (
        "class Store:\n    pass\n\n\n"
        "def add_node(store, name, kind):\n    return (store, name, kind)\n\n\n"
        "def get_node(store, name):\n    return name\n"
    ),
    # Imports the function and calls it.
    "kn/ingest_code.py": (
        "from graph_store import add_node\n\n\n"
        "def ingest(store, path):\n    return add_node(store, path, 'code')\n"
    ),
    # Imports the module and calls through it.
    "kn/ingest_docs.py": (
        "import graph_store\n\n\n"
        "def ingest_doc(store, path):\n"
        "    return graph_store.add_node(store, path, 'doc')\n"
    ),
    # Imports the module, calls something else in it.
    "kn/query.py": (
        "from graph_store import get_node\n\n\n"
        "def lookup(store, name):\n    return get_node(store, name)\n"
    ),
    # Imports the module and calls nothing in it.
    "kn/link_commits.py": (
        "from graph_store import Store\n\n\n"
        "def link(s: Store):\n    return s\n"
    ),
    # Two hops away: calls a caller.
    "kn/cli.py": (
        "from ingest_code import ingest\n\n\n"
        "def main():\n    return ingest(None, 'x')\n"
    ),
    "kn/tests/test_store.py": (
        "from graph_store import add_node\n\n\n"
        "def test_add_node():\n    assert add_node(None, 'a', 'b')\n"
    ),
}


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   stdin=subprocess.DEVNULL)


@pytest.fixture
def callers_repo(tmp_path):
    repo = tmp_path / "kit"
    for rel, text in _FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x")
    # The CLI's build, not full_build alone: `graph_store.add_node(...)`
    # through a module import is resolved by its post-processing.
    import os
    from pathlib import Path

    engine = Path(__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, "-m", "cartograph", "build", "--repo", str(repo),
         "--format", "json"],
        check=True, capture_output=True, stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONPATH": str(engine)},
    )
    return repo


def _impact(repo, capsys, *extra):
    argv = ["cartograph", "impact", "--files", "kn/graph_store.py",
            "--repo", str(repo), "--format", "json", *extra]
    with patch.object(sys, "argv", argv):
        with pytest.raises(SystemExit) as exc:
            cli.main()
    assert exc.value.code == 0
    return json.loads(capsys.readouterr().out)["data"]


def _rows_by_location(data):
    return {row.rsplit(" | ", 1)[-1]: row for row in data["impacted_nodes"]}


def test_each_row_says_how_it_depends_on_the_change(callers_repo, capsys):
    rows = _rows_by_location(_impact(callers_repo, capsys))
    assert rows["kn/ingest_code.py:4"] == "direct | calls add_node | Function | ingest | kn/ingest_code.py:4"
    assert rows["kn/ingest_docs.py:4"].startswith("direct | calls add_node | ")
    # A caller of another function in the same file says which one.
    assert rows["kn/query.py:4"].startswith("direct | calls get_node | ")
    # An importer that calls nothing is an importer, and says so.
    assert rows["kn/link_commits.py"] == "direct | imports graph_store.py | File | kn/link_commits.py"
    assert rows["kn/ingest_code.py"].startswith("direct | imports graph_store.py | File")
    # The test both calls and is recorded as testing it.
    assert rows["kn/tests/test_store.py:4"].startswith(
        "direct | calls add_node; tests add_node | Test | ")
    # Two hops: says what it reached the change through.
    assert rows["kn/cli.py:4"] == "transitive | calls ingest | Function | main | kn/cli.py:4"


def test_the_summary_separates_callers_from_importers(callers_repo, capsys):
    data = _impact(callers_repo, capsys)
    summary = data["summary"]
    assert summary.startswith(
        "graph_store.py: 11 items affected within 2 hops across 6 files "
        "(9 direct: 4 call, 5 import only); all shown")
    assert ("the 5 import-only dependents are not broken by a signature "
            "change, only by a rename or removal") in summary
    assert "carto query callers_of kn/graph_store.py::<name>" in summary
    assert "\n" not in summary
    assert data["totals"]["direct_by_relation"] == {"call": 4, "import only": 5}


def test_file_rows_say_which_files_only_import(callers_repo, capsys):
    files = dict(r.split(" | ", 1) for r in _impact(callers_repo, capsys)["affected_files"])
    assert files["kn/link_commits.py"] == "1 item (1 direct; 1 only imports)"
    assert files["kn/ingest_code.py"] == "2 items (2 direct; 1 only imports)"
    assert files["kn/cli.py"] == "2 items"


def test_the_breakdown_counts_every_direct_dependent_beyond_the_limit(callers_repo, capsys):
    data = _impact(callers_repo, capsys, "--limit", "1")
    assert len(data["impacted_nodes"]) == 1
    assert data["totals"]["direct_by_relation"] == {"call": 4, "import only": 5}
    assert sum(data["totals"]["direct_by_relation"].values()) == data["totals"]["direct"]
    assert "(9 direct: 4 call, 5 import only)" in data["summary"]


def test_full_rows_carry_the_relation(callers_repo, capsys):
    data = _impact(callers_repo, capsys, "--detail", "full")
    by_name = {n["name"]: n for n in data["impacted_nodes"]}
    assert by_name["ingest"]["via"] == ["calls add_node"]
    assert by_name["main"]["via"] == ["calls ingest"]


def test_both_traversal_engines_say_the_same(callers_repo, capsys, monkeypatch):
    sql = _impact(callers_repo, capsys, "--detail", "full")
    monkeypatch.setattr(graph_module, "BFS_ENGINE", "networkx")
    nx = _impact(callers_repo, capsys, "--detail", "full")
    assert [n.get("via") for n in nx["impacted_nodes"]] == [
        n.get("via") for n in sql["impacted_nodes"]]
    assert nx["totals"] == sql["totals"]
    assert nx["affected_files"] == sql["affected_files"]
    assert nx["summary"] == sql["summary"]


# --------------------------------------------------------------------------
# A wrong spelling names the right command
# --------------------------------------------------------------------------


def _usage(argv, capsys):
    with patch.object(sys, "argv", ["cartograph", *argv]):
        with pytest.raises(SystemExit) as exc:
            cli.main()
    assert exc.value.code == 1
    env = json.loads(capsys.readouterr().out)
    assert env["ok"] is False and env["error"]["code"] == "usage"
    return env["error"]


def test_query_impact_names_the_impact_command(capsys):
    # The spelling one T5 run tried.
    err = _usage(["query", "impact", "--files", "kn/graph_store.py", "--depth", "2",
                  "--format", "json"], capsys)
    assert err["remediation"] == (
        "carto impact --files kn/graph_store.py --depth 2 --format json")
    assert "`impact` is a command of its own" in err["message"]


def test_query_impact_on_a_symbol_names_the_file_and_callers_of(capsys):
    err = _usage(["query", "impact", "kn/graph_store.py::add_node", "--format", "json"],
                 capsys)
    assert err["remediation"] == "carto impact --files kn/graph_store.py --format json"
    assert "carto query callers_of kn/graph_store.py::add_node" in err["message"]


def test_query_large_functions_names_the_command(capsys):
    err = _usage(["query", "large-functions", "--limit", "10", "--format", "json"], capsys)
    assert err["remediation"] == "carto large-functions --limit 10 --format json"
    err = _usage(["query", "large_functions", "--format", "json"], capsys)
    assert err["remediation"] == "carto large-functions --format json"


def test_a_near_miss_pattern_names_the_pattern(capsys):
    err = _usage(["query", "callers", "add_node", "--format", "json"], capsys)
    assert err["remediation"] == "carto query callers_of add_node --format json"
    err = _usage(["query", "tests-for", "add_node", "--format", "json"], capsys)
    assert err["remediation"] == "carto query tests_for add_node --format json"


def test_a_pattern_used_as_a_command_names_query(capsys):
    err = _usage(["callers_of", "add_node", "--format", "json"], capsys)
    assert err["remediation"] == "carto query callers_of add_node --format json"
