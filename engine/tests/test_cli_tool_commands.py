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
            ["impact", "--files", "a.py", "b.py", "--depth", "3", "--max-results", "20"],
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
                "kind": "Class",
                "file_path_pattern": None,
                "limit": 4,
            },
        ),
        (
            ["refactor", "dead_code", "--kind", "Function", "--path", "src/"],
            "refactor_func",
            {
                "mode": "dead_code",
                "old_name": None,
                "new_name": None,
                "kind": "Function",
                "file_pattern": "src/",
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
def test_tool_commands_reject_invalid_or_ambiguous_arguments(arguments):
    with patch.object(sys, "argv", ["cartograph", *arguments]):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
    assert exc_info.value.code == 2


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
