"""CLI wrappers for graph tools reconciled from PR #95.

Exit-code note (capability contract v1): a missing graph is a PRECONDITION
failure and exits **2**, not the usage exit 1 these tests were written
against. The call was correct; the environment is not ready, and the failure
carries ``carto build`` as a remediation the agent can act on.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest

import cartograph.tools  # noqa: F401 - exposes lazy patch targets
from cartograph import cli


@pytest.mark.parametrize(
    ("arguments", "tool_name", "expected"),
    [
        (
            # `query` gained --detail-level and --limit when the fork closed the
            # CLI/MCP parity gap (PROVENANCE.md, "~12 commands gained missing
            # flags"), so their defaults are now forwarded too.
            ["query", "callers_of", "target"],
            "query_graph",
            {
                "pattern": "callers_of",
                "target": "target",
                "detail_level": "standard",
                "max_results": 100,
            },
        ),
        (
            ["impact", "--files", "a.py", "b.py", "--depth", "3", "--limit", "20"],
            "get_impact_radius",
            {
                "changed_files": ["a.py", "b.py"],
                "max_depth": 3,
                "max_results": 20,
                "base": "HEAD~1",
            },
        ),
        (
            ["search", "login", "--kind", "Function", "--limit", "7"],
            "semantic_search_nodes",
            {"query": "login", "kind": "Function", "limit": 7},
        ),
        (
            ["flows", "--sort", "depth", "--limit", "9", "--kind", "Function"],
            "list_flows",
            {"sort_by": "depth", "limit": 9, "kind": "Function"},
        ),
        (
            ["flow", "--id", "7", "--source"],
            "get_flow",
            {"flow_id": 7, "flow_name": None, "include_source": True},
        ),
        (
            ["communities", "--sort", "cohesion", "--min-size", "3"],
            "list_communities_func",
            {"sort_by": "cohesion", "min_size": 3},
        ),
        (
            ["community", "--name", "parser", "--members"],
            "get_community_func",
            {
                "community_name": "parser",
                "community_id": None,
                "include_members": True,
            },
        ),
        (
            ["architecture", "--detail-level", "standard"],
            "get_architecture_overview_func",
            {"detail_level": "standard"},
        ),
        (
            ["large-functions", "--min-lines", "80", "--kind", "Class", "--limit", "4"],
            "find_large_functions",
            {
                "min_lines": 80,
                # --kind repeats to widen, so a single value arrives as a list.
                "kind": ["Class"],
                "file_path_pattern": None,
                "limit": 4,
                "include_generated": False,
            },
        ),
        (
            # `refactor` gained --limit for the same reason `query` gained its
            # flags: the tool always bounded its response, and the CLI gave
            # the caller no way to say how large that bound should be.
            ["refactor", "dead_code", "--kind", "Function", "--path", "src/"],
            "refactor_func",
            {
                "mode": "dead_code",
                "old_name": None,
                "new_name": None,
                "kind": "Function",
                "file_pattern": "src/",
                "max_results": 50,
            },
        ),
    ],
)
def test_tool_command_forwards_typed_arguments_as_json(
    arguments, tool_name, expected, tmp_path, monkeypatch, capsys,
):
    repo = tmp_path / "repo"
    nested = repo / "src" / "nested"
    nested.mkdir(parents=True)
    (repo / ".git").mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "graph.db").touch()
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    argv = ["cartograph", *arguments, "--repo", str(nested)]
    result = {"status": "ok", "tool": tool_name}

    with patch.object(sys, "argv", argv):
        with patch(f"cartograph.tools.{tool_name}", return_value=result) as tool:
            # Graph-tool commands now terminate by raising SystemExit(emit(...)):
            # the envelope IS the return value, so there is no plain return path.
            with pytest.raises(SystemExit) as exc_info:
                cli.main()

    assert exc_info.value.code == 0
    # The tool's own dict is no longer stdout on its own — it is carried as the
    # envelope's `data`, which is the only thing json mode may write to stdout.
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == 1
    assert payload["ok"] is True
    # `tool` is the logical operation (the CLI subcommand), stable across
    # renames — deliberately not the underlying cartograph.tools function name.
    assert payload["tool"] == arguments[0]
    assert payload["data"] == result
    tool.assert_called_once_with(repo_root=str(repo), **expected)


@pytest.mark.parametrize(
    "arguments",
    [
        ["flow"],
        ["flow", "--id", "1", "--name", "duplicate"],
        ["community"],
        ["community", "--id", "1", "--name", "duplicate"],
        ["refactor", "rename", "--old-name", "only-old"],
        ["impact", "--depth", "-1"],
        ["search", "query", "--limit", "0"],
    ],
)
def test_tool_commands_reject_invalid_or_ambiguous_arguments(arguments, capsys):
    """A malformed call is USAGE (exit 1) and says so in an envelope.

    This asserted exit 2 and passed, because 2 is what argparse does — but 2
    is PRECONDITION here, so an agent read a bad flag value as "no graph",
    built one it did not need, and retried the identical bad call. The exit
    code is now 1 and stdout carries the usage envelope, so stderr prose is no
    longer the only account of what went wrong.
    """
    with patch.object(sys, "argv", ["cartograph", *arguments]):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
    assert exc_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "usage"
    assert payload["tool"] == arguments[0]


def test_tool_command_missing_graph_reports_a_recoverable_precondition(
    tmp_path, monkeypatch, capsys,
):
    """A missing graph is a precondition the agent can self-heal from.

    Exit 2 (not 1), and — because graph-tool commands default to json — the
    failure arrives as an envelope on STDOUT carrying a machine-readable
    remediation, so an agent parsing json never has to scrape stderr. The
    pre-fork test looked for bare prose on stderr; that path now only runs
    under ``--format text``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setenv("CRG_DATA_DIR", str(tmp_path / "missing"))

    with patch.object(
        sys,
        "argv",
        ["cartograph", "query", "callers_of", "target", "--repo", str(repo)],
    ):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()

    assert exc_info.value.code == 2  # precondition, not usage: no graph
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["tool"] == "query"
    assert payload["error"]["code"] == "precondition"
    assert "No graph found" in payload["error"]["message"]
    # Required for precondition errors: it is what lets the agent recover
    # instead of failing the user's task.
    assert payload["error"]["remediation"] == "carto build"


@pytest.mark.parametrize("detail", ["compact", "full"])
def test_callers_of_lists_one_edge_per_call_line(
    tmp_path, monkeypatch, capsys, detail,
):
    """A caller with three call lines is one result and three edges (full).

    Measured on a real repo: `_build_fixture` called `add_node` on four lines
    and the response showed one, so an agent reported three call sites where
    there were seven. Built from source, so the parser's one-edge-per-line
    storage is exercised along with the query and both output shapes.
    """
    from cartograph.graph import GraphStore
    from cartograph.incremental import full_build

    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "lib.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "use.py").write_text(
        "from lib import add\n"
        "\n"
        "\n"
        "def many():\n"
        "    x = add(1, 2)\n"
        "    y = add(3, 4)\n"
        "    return add(x, y)\n"
        "\n"
        "\n"
        "def once():\n"
        "    return add(8, 9)\n",
        encoding="utf-8",
    )
    db_path = repo / ".cartograph" / "graph.db"
    db_path.parent.mkdir()
    store = GraphStore(db_path)
    try:
        full_build(repo, store)
    finally:
        store.close()

    argv = ["cartograph", "query", "callers_of", "lib.py::add",
            "--repo", str(repo), "--detail", detail]
    with patch.object(sys, "argv", argv):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()

    assert exc_info.value.code == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["result_count"] == 2
    assert data["results_omitted"] == 0
    assert len(data["results"]) == 2
    assert data["summary"] == (
        "Found 2 caller(s), 4 call line(s) for callers_of('lib.py::add')"
    )
    if detail == "compact":
        assert data["results"] == [
            "Function | many | use.py:4 | calls at 5, 6, 7",
            "Function | once | use.py:10 | calls at 11",
        ]
        # The rows state every call line; the edges would say them again.
        assert "edges" not in data
    else:
        assert [(r["qualified_name"], r["call_lines"]) for r in data["results"]] == [
            ("use.py::many", [5, 6, 7]), ("use.py::once", [11]),
        ]
        assert [(e["source"], e["line"]) for e in data["edges"]] == [
            ("use.py::many", 5), ("use.py::many", 6), ("use.py::many", 7),
            ("use.py::once", 11),
        ]


def _build_repo(tmp_path, monkeypatch, files: dict[str, str]):
    """A repository built from source, so edges are stored as the parser stores them."""
    from cartograph.graph import GraphStore
    from cartograph.incremental import full_build

    monkeypatch.setenv("CRG_SERIAL_PARSE", "1")
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    for name, text in files.items():
        (repo / name).write_text(text, encoding="utf-8")
    db_path = repo / ".cartograph" / "graph.db"
    db_path.parent.mkdir()
    store = GraphStore(db_path)
    try:
        full_build(repo, store)
    finally:
        store.close()
    return repo


def _run_query(capsys, repo, *extra: str) -> dict:
    argv = ["cartograph", "query", *extra, "--repo", str(repo)]
    with patch.object(sys, "argv", argv):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
    assert exc_info.value.code == 0
    return json.loads(capsys.readouterr().out)


_CALLERS_REPO = {
    "lib.py": "def add(a, b):\n    return a + b\n",
    "use.py": (
        "from lib import add\n"
        "\n"
        "\n"
        "def many():\n"
        "    x = add(1, 2)\n"
        "    y = add(3, 4)\n"
        "    return add(x, y)\n"
        "\n"
        "\n"
        "def once():\n"
        "    return add(8, 9)\n"
    ),
}


def _two_pages(capsys, repo, pattern: str, target: str, detail: str):
    first = _run_query(capsys, repo, pattern, target, "--limit", "1", "--detail", detail)
    cursor = first["page"]["next_cursor"]
    assert cursor
    second = _run_query(
        capsys, repo, pattern, target, "--limit", "1", "--detail", detail,
        "--cursor", cursor,
    )
    return first, second


def test_each_callers_of_page_carries_only_its_own_callers_edges(
    tmp_path, monkeypatch, capsys,
):
    """Page 2 repeated page 1's call lines: edges were never sliced by cursor."""
    repo = _build_repo(tmp_path, monkeypatch, _CALLERS_REPO)
    first, second = _two_pages(capsys, repo, "callers_of", "lib.py::add", "full")

    assert [r["qualified_name"] for r in first["data"]["results"]] == ["use.py::many"]
    assert [(e["source"], e["line"]) for e in first["data"]["edges"]] == [
        ("use.py::many", 5), ("use.py::many", 6), ("use.py::many", 7),
    ]
    assert [r["qualified_name"] for r in second["data"]["results"]] == ["use.py::once"]
    assert [(e["source"], e["line"]) for e in second["data"]["edges"]] == [
        ("use.py::once", 11),
    ]
    # The parallel row index is how the CLI pages edges; it is never emitted.
    assert "_edge_rows" not in first["data"]
    assert "_edge_rows" not in second["data"]


def test_paging_slices_edges_for_every_pattern_whose_edges_belong_to_rows(
    tmp_path, monkeypatch, capsys,
):
    """The slicing is not callers_of's: inheritors_of paged the same way."""
    repo = _build_repo(tmp_path, monkeypatch, {
        "shapes.py": (
            "class Base:\n    pass\n\n\n"
            "class One(Base):\n    pass\n\n\n"
            "class Two(Base):\n    pass\n"
        ),
    })
    first, second = _two_pages(capsys, repo, "inheritors_of", "shapes.py::Base", "full")

    rows = [first["data"]["results"][0]["name"], second["data"]["results"][0]["name"]]
    assert sorted(rows) == ["One", "Two"]
    for page, name in ((first, rows[0]), (second, rows[1])):
        assert [e["source"].rsplit("::", 1)[-1] for e in page["data"]["edges"]] == [name]


def test_compact_callers_of_carries_no_edges_its_rows_already_state(
    tmp_path, monkeypatch, capsys,
):
    """Every row ends `calls at <lines>`; the edge list said it all again."""
    repo = _build_repo(tmp_path, monkeypatch, _CALLERS_REPO)
    data = _run_query(capsys, repo, "callers_of", "lib.py::add")["data"]

    assert data["results"] == [
        "Function | many | use.py:4 | calls at 5, 6, 7",
        "Function | once | use.py:10 | calls at 11",
    ]
    assert "edges" not in data

    first, second = _two_pages(capsys, repo, "callers_of", "lib.py::add", "compact")
    assert "edges" not in first["data"] and "edges" not in second["data"]


def test_full_callers_of_keeps_its_edges(tmp_path, monkeypatch, capsys):
    repo = _build_repo(tmp_path, monkeypatch, _CALLERS_REPO)
    data = _run_query(
        capsys, repo, "callers_of", "lib.py::add", "--detail", "full",
    )["data"]
    assert [(e["source"], e["line"]) for e in data["edges"]] == [
        ("use.py::many", 5), ("use.py::many", 6), ("use.py::many", 7),
        ("use.py::once", 11),
    ]


def test_compact_keeps_edges_for_patterns_whose_rows_do_not_state_them(
    tmp_path, monkeypatch, capsys,
):
    """Only callers_of and callees_of rows carry their call lines."""
    repo = _build_repo(tmp_path, monkeypatch, {
        "shapes.py": "class Base:\n    pass\n\n\nclass One(Base):\n    pass\n",
    })
    data = _run_query(capsys, repo, "inheritors_of", "shapes.py::Base")["data"]
    assert len(data["edges"]) == 1


def test_an_edge_without_a_row_is_carried_by_the_first_page_only():
    result = {
        "results": ["a", "b", "c"],
        "edges": ["edge-a", "orphan", "edge-b", "edge-c1", "edge-c2"],
        "_edge_rows": [0, None, 1, 2, 2],
    }
    cli._drop_earlier_edges(result, 2)
    assert result["edges"] == ["edge-c1", "edge-c2"]

    # An index that does not line up with the edges cannot say which to keep.
    skewed = {"edges": ["x", "y"], "_edge_rows": [0]}
    cli._drop_earlier_edges(skewed, 1)
    assert "edges" not in skewed


def test_a_token_budget_never_emits_the_edge_index(tmp_path, monkeypatch, capsys):
    repo = _build_repo(tmp_path, monkeypatch, _CALLERS_REPO)
    for detail in ("compact", "full"):
        env = _run_query(
            capsys, repo, "callers_of", "lib.py::add", "--limit", "1",
            "--detail", detail, "--max-tokens", "120",
        )
        assert "_edge_rows" not in json.dumps(env)


def test_a_heuristically_resolved_call_says_so_on_its_row():
    """Compact callers_of drops edges, which were the only place a tier showed."""
    from types import SimpleNamespace

    from cartograph import compact
    from cartograph.tools.query import _with_call_lines

    def edge(line, tier):
        return SimpleNamespace(file_path="use.py", line=line, confidence_tier=tier)

    row = {"kind": "Function", "name": "f", "qualified_name": "use.py::f",
           "file_path": "use.py", "line_start": 1}
    inferred = _with_call_lines(dict(row), [edge(3, "EXTRACTED"), edge(4, "INFERRED")])
    assert inferred["call_confidence"] == "INFERRED"
    assert compact.node_row(inferred) == "Function | f | use.py:1 | calls at 3, 4 | INFERRED"

    extracted = _with_call_lines(dict(row), [edge(3, "EXTRACTED")])
    assert "call_confidence" not in extracted
    assert compact.node_row(extracted) == "Function | f | use.py:1 | calls at 3"


_CALLEES_REPO = {
    "lib.py": "def add(a, b):\n    return a + b\n",
    "use.py": (
        "from lib import add\n"
        "\n"
        "\n"
        "def helper():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def run():\n"
        "    a = add(1, 2)\n"
        "    b = helper()\n"
        "    c = add(a, b)\n"
        "    external(c)\n"
        "    external(a)\n"
        "    return c\n"
    ),
}


def test_callees_of_rows_say_where_each_callee_is_called(
    tmp_path, monkeypatch, capsys,
):
    """One row per callee, ending `calls at`, as callers_of rows do.

    A row says where the callee is defined; the call lines are in the queried
    function's file, so a callee defined elsewhere names that file with each
    line rather than letting a bare number read as a line of its own file.
    """
    repo = _build_repo(tmp_path, monkeypatch, _CALLEES_REPO)
    data = _run_query(capsys, repo, "callees_of", "use.py::run")["data"]

    assert data["summary"] == (
        "Found 3 callee(s), 5 call line(s) for callees_of('use.py::run')"
    )
    assert data["results"] == [
        "Function | add | lib.py:1 | called at use.py:9, 11",
        "Function | helper | use.py:4 | calls at 10",
        "Function | external | not in graph | called at use.py:12, 13",
    ]
    assert "edges" not in data


def test_full_callees_of_keeps_every_call_line_and_edge(
    tmp_path, monkeypatch, capsys,
):
    """It kept one edge per callee: `add` called twice showed one call."""
    repo = _build_repo(tmp_path, monkeypatch, _CALLEES_REPO)
    data = _run_query(
        capsys, repo, "callees_of", "use.py::run", "--detail", "full",
    )["data"]

    by_name = {r["qualified_name"]: r for r in data["results"]}
    assert by_name["use.py::helper"]["call_lines"] == [10]
    assert by_name["lib.py::add"]["call_lines"] == []
    assert by_name["lib.py::add"]["call_sites_elsewhere"] == [
        {"file": "use.py", "line": 9}, {"file": "use.py", "line": 11},
    ]
    # An external callee keeps its bare name and gets its call lines too.
    external = by_name["external"]
    assert external["kind"] == "Function" and "file_path" not in external
    assert external["call_sites_elsewhere"] == [
        {"file": "use.py", "line": 12}, {"file": "use.py", "line": 13},
    ]
    assert [(e["target"], e["line"]) for e in data["edges"]] == [
        ("lib.py::add", 9), ("lib.py::add", 11), ("use.py::helper", 10),
        ("external", 12), ("external", 13),
    ]


def test_each_callees_of_page_carries_only_its_own_callees_edges(
    tmp_path, monkeypatch, capsys,
):
    repo = _build_repo(tmp_path, monkeypatch, _CALLEES_REPO)
    first, second = _two_pages(capsys, repo, "callees_of", "use.py::run", "full")

    assert [(e["target"], e["line"]) for e in first["data"]["edges"]] == [
        ("lib.py::add", 9), ("lib.py::add", 11),
    ]
    assert [(e["target"], e["line"]) for e in second["data"]["edges"]] == [
        ("use.py::helper", 10),
    ]
    # Both totals cover every callee, not just the page.
    assert second["data"]["summary"].startswith(
        "Found 3 callee(s), 5 call line(s) for callees_of("
    )
