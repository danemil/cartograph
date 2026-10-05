"""Current-main regressions for the reconciled CLI contribution stack.

Exit-code note (capability contract v1): a missing graph is a PRECONDITION
failure and exits **2**, not the usage exit 1 these tests were written
against. The call was correct; the environment is not ready, and the failure
carries ``carto build`` as a remediation the agent can act on.
"""

from __future__ import annotations

import json
import logging
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import cartograph.graph  # noqa: F401 - imported so unittest.mock can patch it
from cartograph import cli
from cartograph.incremental import PARSER_VERSION


@pytest.mark.parametrize(
    ("command", "result"),
    [
        (
            "build",
            {"files_parsed": 1, "total_nodes": 2, "total_edges": 1},
        ),
        (
            "update",
            {"files_updated": 1, "total_nodes": 2, "total_edges": 1},
        ),
    ],
)
def test_quiet_build_and_update_suppress_summary_and_info_logs(
    command, result, capsys, caplog,
):
    """``--quiet`` must silence progress logs as well as the final summary."""

    def _run_with_progress(**_kwargs):
        logging.getLogger("cartograph.test_progress").info("parsing progress")
        return result

    argv = ["cartograph", command, "--repo", "repo-root", "--quiet"]
    with caplog.at_level(logging.INFO):
        with patch.object(sys, "argv", argv):
            with patch("cartograph.graph.GraphStore", return_value=MagicMock()):
                with patch(
                    "cartograph.incremental.get_db_path",
                    return_value=MagicMock(),
                ):
                    with patch(
                        "cartograph.tools.build.build_or_update_graph",
                        side_effect=_run_with_progress,
                    ):
                        cli.main()

    assert capsys.readouterr().out == ""
    assert "parsing progress" not in caplog.text


def test_status_json_is_the_only_stdout_and_includes_current_sha(capsys):
    store = MagicMock()
    store.get_stats.return_value = SimpleNamespace(
        total_nodes=3,
        total_edges=4,
        files_count=2,
        languages=["Python"],
        last_updated="2026-07-17T12:00:00Z",
    )
    store.get_metadata.side_effect = {
        "git_branch": "main",
        "git_head_sha": "old-sha",
        "svn_revision": None,
        "svn_branch": None,
        "parser_version": PARSER_VERSION,
    }.get
    argv = ["cartograph", "status", "--repo", "repo-root", "--json"]

    with patch.object(sys, "argv", argv):
        with patch("cartograph.graph.GraphStore", return_value=store):
            with patch(
                "cartograph.incremental.get_db_path",
                return_value=MagicMock(),
            ):
                with patch(
                    "cartograph.incremental.detect_vcs",
                    return_value="git",
                ):
                    with patch(
                        "cartograph.incremental._git_branch_info",
                        return_value=("feature", "current-sha"),
                    ):
                        cli.main()

    captured = capsys.readouterr()
    # "The only stdout" is still the point, but the envelope is pretty-printed,
    # so counting newlines no longer expresses it. Parsing the WHOLE of stdout
    # as one document does: any stray print would make this raise.
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["schema"] == 1
    assert payload["ok"] is True
    assert payload["tool"] == "status"
    assert payload["data"] == {
        "nodes": 3,
        "edges": 4,
        "files": 2,
        "languages": ["Python"],
        "last_updated": "2026-07-17T12:00:00Z",
        "vcs": "git",
        "built_on_branch": "main",
        "built_at_commit": "old-sha",
        "current_branch": "feature",
        "current_sha": "current-sha",
        "svn_branch": None,
        "svn_revision": None,
        # New under the envelope: said outright rather than left for the agent
        # to infer by comparing built_on_branch against current_branch.
        "stale": True,
        "stale_reason": "built on branch 'main', now on 'feature'",
        "remediation": "carto build",
    }
    assert payload["provenance"] == {
        "graph_sha": "old-sha",
        "built_at": "2026-07-17T12:00:00Z",
    }


def test_status_quiet_prints_nothing(capsys):
    store = MagicMock()
    store.get_stats.return_value = SimpleNamespace(
        total_nodes=0,
        total_edges=0,
        files_count=0,
        languages=[],
        last_updated=None,
    )
    store.get_metadata.return_value = None
    argv = ["cartograph", "status", "--repo", "repo-root", "--quiet"]

    with patch.object(sys, "argv", argv):
        with patch("cartograph.graph.GraphStore", return_value=store):
            with patch(
                "cartograph.incremental.get_db_path",
                return_value=MagicMock(),
            ):
                with patch("cartograph.incremental.detect_vcs", return_value="none"):
                    cli.main()

    assert capsys.readouterr().out == ""


def test_status_missing_graph_exits_without_creating_data_tree(
    tmp_path, monkeypatch, capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    data_dir = tmp_path / "missing-data"
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    argv = ["cartograph", "status", "--repo", str(repo)]

    with patch.object(sys, "argv", argv):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()

    assert exc_info.value.code == 2  # precondition, not usage: no graph
    assert "No graph found" in capsys.readouterr().err
    assert not data_dir.exists()


@pytest.mark.parametrize(
    "command",
    ["status", "detect-changes", "visualize", "wiki", "watch"],
)
def test_read_only_commands_missing_graph_do_not_create_empty_db(
    command, tmp_path, monkeypatch, capsys,
):
    """#803: read-only consumers must not poison a repo with empty graph.db."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    # detect-changes requires a real git worktree for root discovery.
    if command == "detect-changes":
        import subprocess

        subprocess.run(
            ["git", "-C", str(repo), "init", "-q"],
            check=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )
    data_dir = tmp_path / "missing-data"
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    monkeypatch.delenv("CRG_HOME", raising=False)
    argv = ["cartograph", command, "--repo", str(repo)]

    with patch(
        "cartograph.registry.default_registry_path",
        return_value=tmp_path / "missing-registry.json",
    ):
        with patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()

    assert exc_info.value.code == 2  # precondition, not usage: no graph
    # detect-changes defaults to json now that it is inside the contract, so
    # its precondition arrives as an envelope on stdout; the text-default
    # commands still put prose on stderr. What this test is about is that the
    # graph is named as missing and nothing is materialized either way.
    captured = capsys.readouterr()
    assert "No graph found" in captured.err + captured.out
    assert not data_dir.exists()
    assert not (repo / ".cartograph").exists()


@pytest.mark.parametrize("command", ["visualize", "wiki", "watch", "status"])
def test_read_only_commands_data_dir_option_is_read_only(
    command, tmp_path, monkeypatch, capsys,
):
    """Explicit --data-dir must not create dirs/registry when the graph is missing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    data_dir = tmp_path / "explicit-data"
    registry_path = tmp_path / "registry" / "registry.json"
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    argv = [
        "cartograph",
        command,
        "--repo",
        str(repo),
        "--data-dir",
        str(data_dir),
    ]

    with patch(
        "cartograph.registry.default_registry_path",
        return_value=registry_path,
    ):
        with patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()

    assert exc_info.value.code == 2  # precondition, not usage: no graph
    assert "No graph found" in capsys.readouterr().err
    assert not data_dir.exists()
    assert not registry_path.exists()


def test_status_preserves_legacy_graph_migration(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    legacy_db = repo / ".code-review-graph.db"
    with cartograph.graph.GraphStore(legacy_db):
        pass
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    argv = ["cartograph", "status", "--repo", str(repo)]

    with patch(
        "cartograph.registry.default_registry_path",
        return_value=tmp_path / "missing-registry.json",
    ):
        with patch.object(sys, "argv", argv):
            cli.main()

    assert "Nodes: 0" in capsys.readouterr().out
    assert not legacy_db.exists()
    assert (repo / ".cartograph" / "graph.db").exists()


def test_status_external_data_dir_does_not_migrate_unrelated_legacy_graph(
    tmp_path, monkeypatch, capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    legacy_db = repo / ".code-review-graph.db"
    with cartograph.graph.GraphStore(legacy_db):
        pass
    data_dir = tmp_path / "external-data"
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    argv = ["cartograph", "status", "--repo", str(repo)]

    with patch.object(sys, "argv", argv):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()

    assert exc_info.value.code == 2  # precondition, not usage: no graph
    assert "No graph found" in capsys.readouterr().err
    assert legacy_db.exists()
    assert not data_dir.exists()


def test_status_data_dir_option_is_read_only(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    data_dir = tmp_path / "explicit-data"
    registry_path = tmp_path / "registry" / "registry.json"
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    argv = [
        "cartograph",
        "status",
        "--repo",
        str(repo),
        "--data-dir",
        str(data_dir),
    ]

    with patch(
        "cartograph.registry.default_registry_path",
        return_value=registry_path,
    ):
        with patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit) as exc_info:
                cli.main()

    assert exc_info.value.code == 2  # precondition, not usage: no graph
    assert "No graph found" in capsys.readouterr().err
    assert not data_dir.exists()
    assert not registry_path.exists()

    with cartograph.graph.GraphStore(data_dir / "graph.db"):
        pass
    with patch(
        "cartograph.registry.default_registry_path",
        return_value=registry_path,
    ):
        with patch.object(sys, "argv", argv):
            cli.main()

    assert "Nodes: 0" in capsys.readouterr().out
    assert not registry_path.exists()


def test_status_default_data_dir_override_does_not_migrate_legacy_graph(
    tmp_path, monkeypatch, capsys,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    legacy_db = repo / ".code-review-graph.db"
    with cartograph.graph.GraphStore(legacy_db):
        pass
    data_dir = repo / ".cartograph"
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    argv = [
        "cartograph",
        "status",
        "--repo",
        str(repo),
        "--data-dir",
        str(data_dir),
    ]

    with patch.object(sys, "argv", argv):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()

    assert exc_info.value.code == 2  # precondition, not usage: no graph
    assert "No graph found" in capsys.readouterr().err
    assert legacy_db.exists()
    assert not data_dir.exists()


def _dead_items():
    return [
        {
            "name": name,
            "qualified_name": f"src/app.py::{name}",
            "kind": "Function",
            "file": "src/app.py",
            "file_path": "src/app.py",
            "relative_path": "src/app.py",
            "line": line,
            "language": "python",
        }
        for line, name in enumerate(("one", "two", "three"), start=1)
    ]


def test_dead_code_uses_project_root_external_data_and_reports_total(
    tmp_path, monkeypatch, capsys,
):
    repo = tmp_path / "repo"
    subdir = repo / "src" / "nested"
    subdir.mkdir(parents=True)
    (repo / ".git").mkdir()
    data_dir = tmp_path / "external-data"
    data_dir.mkdir()
    db_path = data_dir / "graph.db"
    db_path.touch()
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    store = MagicMock()
    argv = [
        "cartograph",
        "dead-code",
        "--repo",
        str(subdir),
        "--limit",
        "2",
    ]

    with patch.object(sys, "argv", argv):
        with patch("cartograph.graph.GraphStore", return_value=store) as graph_store:
            with patch(
                "cartograph.refactor.find_dead_code",
                return_value=_dead_items(),
            ) as find_dead:
                cli.main()

    output = capsys.readouterr().out
    graph_store.assert_called_once_with(db_path)
    find_dead.assert_called_once_with(store, kind=None, file_pattern=None, root=repo)
    assert "Dead code: 3 item(s); showing 2" in output
    assert "one" in output and "two" in output and "three" not in output


def test_dead_code_json_limit_is_machine_readable(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "graph.db").touch()
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    argv = [
        "cartograph",
        "dead-code",
        "--repo",
        str(repo),
        "--json",
        "--limit",
        "1",
        # The whole row, so the items can be compared field for field; the
        # compact default is covered in test_compact_output.py.
        "--detail",
        "full",
    ]

    with patch.object(sys, "argv", argv):
        with patch("cartograph.graph.GraphStore", return_value=MagicMock()):
            with patch(
                "cartograph.refactor.find_dead_code",
                return_value=_dead_items(),
            ):
                with pytest.raises(SystemExit) as exc_info:
                    cli.main()

    # Was a bare JSON array, which the envelope schema does not permit as
    # `data` and which carried no size, provenance or paging. `--json` now
    # means here what it means everywhere else, and `--limit` is reported as
    # the page it actually is.
    assert exc_info.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"] == "dead-code"
    assert payload["data"]["items"] == _dead_items()[:1]
    assert payload["data"]["total"] == len(_dead_items())
    assert payload["page"]["limit"] == 1
    assert payload["page"]["result_count"] == 1
    assert payload["page"]["has_more"] is True


@pytest.mark.parametrize(
    "extra_args",
    [
        ["--kind", "Module"],
        ["--limit", "-1"],
    ],
)
def test_dead_code_rejects_invalid_filters(extra_args, capsys):
    """A rejected filter is USAGE (exit 1), not the precondition exit 2.

    argparse's own exit 2 collides with PRECONDITION in this protocol, which
    told an agent to run `carto build` over a filter it had merely misspelled.
    """
    argv = ["cartograph", "dead-code", *extra_args, "--format", "json"]
    with patch.object(sys, "argv", argv):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()
    assert exc_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"] == "dead-code"
    assert payload["error"]["code"] == "usage"


def test_dead_code_missing_graph_exits_nonzero(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setenv("CRG_DATA_DIR", str(tmp_path / "missing-data"))
    argv = ["cartograph", "dead-code", "--repo", str(repo)]

    with patch.object(sys, "argv", argv):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()

    assert exc_info.value.code == 2  # precondition, not usage: no graph
    assert "No graph found" in capsys.readouterr().err
