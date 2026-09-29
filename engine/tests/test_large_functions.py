"""What `large-functions` ranks, and what it leaves out.

A measurement on a 70-file repository found `carto large-functions` costing
~6x what `find | xargs wc -l | sort` did for the same question, and returning
no functions at all: every row was a File, Class or Test node, one of them a
generated 14,726-line `.d.ts`. These tests pin the fix — functions and
methods by default, generated files out and counted, one line of summary.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest

from cartograph import cli
from cartograph.graph import GraphStore
from cartograph.incremental import get_db_path, is_generated_file
from cartograph.parser import NodeInfo
from cartograph.tools.query import find_large_functions


# --------------------------------------------------------------------------
# large-functions: kinds and generated files
# --------------------------------------------------------------------------


@pytest.fixture()
def sized_repo(tmp_path):
    """A graph with one of each kind, a method, and a generated declaration file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    store = GraphStore(get_db_path(repo))
    src = str(repo / "src" / "store.ts")
    dts = str(repo / "types" / "worker-configuration.d.ts")
    nodes = [
        NodeInfo(kind="File", name=src, file_path=src, line_start=1, line_end=900,
                 language="typescript"),
        NodeInfo(kind="Class", name="Store", file_path=src, line_start=10,
                 line_end=800, language="typescript"),
        NodeInfo(kind="Function", name="migrate", file_path=src, line_start=20,
                 line_end=320, language="typescript", parent_name="Store"),
        NodeInfo(kind="Function", name="helper", file_path=src, line_start=400,
                 line_end=500, language="typescript"),
        NodeInfo(kind="Function", name="tiny", file_path=src, line_start=600,
                 line_end=605, language="typescript"),
        NodeInfo(kind="Test", name="describe:Store@L1", file_path=src,
                 line_start=820, line_end=899, language="typescript", is_test=True),
        NodeInfo(kind="File", name=dts, file_path=dts, line_start=1,
                 line_end=14726, language="typescript"),
        NodeInfo(kind="Function", name="fetch", file_path=dts, line_start=5,
                 line_end=2000, language="typescript"),
    ]
    for node in nodes:
        store.upsert_node(node)
    store.commit()
    store.close()
    return repo


def test_default_returns_functions_and_methods_only(sized_repo):
    result = find_large_functions(min_lines=50, repo_root=str(sized_repo))
    kinds = {r["kind"] for r in result["results"]}
    names = [r["name"] for r in result["results"]]
    assert kinds == {"Function"}
    # A method is a Function node with a parent; it must not be lost.
    assert names == ["migrate", "helper"]


def test_generated_files_are_excluded_and_counted(sized_repo):
    result = find_large_functions(min_lines=50, repo_root=str(sized_repo))
    assert all(".d.ts" not in r["file_path"] for r in result["results"])
    assert result["excluded"] == {
        "generated_files": 1,
        "include_with": "--include-generated",
    }


def test_generated_files_can_be_included(sized_repo):
    result = find_large_functions(
        min_lines=50, include_generated=True, repo_root=str(sized_repo),
    )
    assert [r["name"] for r in result["results"]][0] == "fetch"
    assert "excluded" not in result


def test_kind_widens_to_several_kinds(sized_repo):
    result = find_large_functions(
        min_lines=50, kind=["Function", "Class", "File"], repo_root=str(sized_repo),
    )
    assert [r["kind"] for r in result["results"]] == ["File", "Class", "Function", "Function"]


def test_a_single_kind_string_is_still_accepted(sized_repo):
    result = find_large_functions(min_lines=50, kind="Test", repo_root=str(sized_repo))
    assert [r["kind"] for r in result["results"]] == ["Test"]


def test_the_limit_counts_rows_after_exclusion(sized_repo):
    """Exclusion happens before the cap, or a page could come back short."""
    result = find_large_functions(min_lines=50, limit=1, repo_root=str(sized_repo))
    assert [r["name"] for r in result["results"]] == ["migrate"]


def test_summary_is_one_line_naming_the_largest(sized_repo):
    result = find_large_functions(min_lines=50, repo_root=str(sized_repo))
    summary = result["summary"]
    assert "\n" not in summary
    assert summary.startswith("2 functions >= 50 lines; largest: Store.migrate (301 lines)")
    # The exclusion is said where an agent reads first, with the way back.
    assert "--include-generated" in summary


def test_summary_when_nothing_qualifies(sized_repo):
    result = find_large_functions(min_lines=5000, repo_root=str(sized_repo))
    assert result["results"] == []
    assert "\n" not in result["summary"]
    assert result["summary"].startswith("No functions >= 5000 lines")


@pytest.mark.parametrize(
    ("path", "generated"),
    [
        ("workers/worker-configuration.d.ts", True),
        ("src/types.d.mts", True),
        ("static/app.min.js", True),
        ("package-lock.json", True),
        ("node_modules/x/index.js", True),
        ("src/store.ts", False),
        ("src/dts.ts", False),
    ],
)
def test_is_generated_file_reuses_the_build_ignore_list(path, generated):
    assert is_generated_file(path) is generated


# --------------------------------------------------------------------------
# Through the CLI
# --------------------------------------------------------------------------


def _run(argv, capsys):
    with patch.object(sys, "argv", ["cartograph", *argv]):
        with pytest.raises(SystemExit) as exc:
            cli.main()
    assert exc.value.code == 0
    return json.loads(capsys.readouterr().out)


def test_large_functions_cli_emits_compact_rows_by_default(sized_repo, capsys):
    env = _run(["large-functions", "--min-lines", "50", "--repo", str(sized_repo)], capsys)
    assert env["data"]["results"] == [
        "301 lines | Function | Store.migrate | src/store.ts:20",
        "101 lines | Function | helper | src/store.ts:400",
    ]
    assert env["page"]["result_count"] == 2


def test_large_functions_cli_detail_full_keeps_the_whole_row(sized_repo, capsys):
    env = _run(
        ["large-functions", "--min-lines", "50", "--detail", "full",
         "--repo", str(sized_repo)],
        capsys,
    )
    row = env["data"]["results"][0]
    assert row["qualified_name"] == "src/store.ts::Store.migrate"
    assert row["line_count"] == 301


def test_large_functions_cli_kind_is_repeatable(sized_repo, capsys):
    env = _run(
        ["large-functions", "--min-lines", "50", "--kind", "Class", "--kind", "File",
         "--repo", str(sized_repo)],
        capsys,
    )
    assert env["data"]["results"] == [
        "900 lines | File | src/store.ts",
        "791 lines | Class | Store | src/store.ts:10",
    ]


def test_large_functions_cli_include_generated(sized_repo, capsys):
    env = _run(
        ["large-functions", "--min-lines", "50", "--include-generated",
         "--repo", str(sized_repo)],
        capsys,
    )
    assert env["data"]["results"][0] == (
        "1996 lines | Function | fetch | types/worker-configuration.d.ts:5"
    )
