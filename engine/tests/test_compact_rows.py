"""Compact rows — one line per row, the default for list commands.

A graph node serialised whole is twelve fields, four of them often the same
path. These tests pin the shortened shape, the one flag that restores the
whole row, and the envelope signals that must survive the shortening.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest

import cartograph.tools  # noqa: F401 - exposes lazy patch targets
from cartograph import cli, compact

# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------


def test_node_row_keeps_what_an_agent_acts_on():
    node = {
        "id": 7, "kind": "Function", "name": "migrate",
        "qualified_name": "src/store.ts::Store.migrate",
        "file_path": "src/store.ts", "line_start": 20, "line_end": 320,
        "language": "typescript", "parent_name": "Store", "is_test": False,
        "line_count": 301, "relative_path": "src/store.ts",
    }
    assert (
        compact.node_row(node, metric=("line_count", "lines"))
        == "301 lines | Function | Store.migrate | src/store.ts:20"
    )


def test_a_caller_row_ends_with_its_resolution():
    # Without the label a caller matched by name alone, or reached through an
    # interface, reads exactly like a resolved one.
    caller = {
        "kind": "Function", "name": "handle",
        "qualified_name": "src/app/Service.java::Service.handle",
        "file_path": "src/app/Service.java", "line_start": 6, "line_end": 9,
        "parent_name": "Service", "target_resolution": "via_supertype",
    }
    assert compact.node_row(caller) == (
        "Function | Service.handle | src/app/Service.java:6 | via_supertype"
    )
    indirect = {**caller, "via": "src/app/Service.java::Service.run",
                "target_resolution": "ambiguous"}
    assert compact.node_row(indirect).endswith(" | via Service.run | ambiguous")
    resolved = {k: v for k, v in caller.items() if k != "target_resolution"}
    assert compact.node_row(resolved) == "Function | Service.handle | src/app/Service.java:6"


def test_a_file_row_does_not_repeat_its_path():
    node = {
        "kind": "File", "name": "src/store.ts", "qualified_name": "src/store.ts",
        "file_path": "src/store.ts", "line_start": 1, "line_end": 900,
    }
    assert compact.node_row(node) == "File | src/store.ts"


def test_a_review_item_is_a_node_row_too():
    item = {
        "id": "src/store.ts::Store.migrate", "title": "migrate", "kind": "Function",
        "location": {"file": "src/store.ts", "line_start": 20, "line_end": 320},
        "metadata": {"language": "typescript", "parent": "Store"},
    }
    assert compact.node_row(item) == "Function | Store.migrate | src/store.ts:20"


def test_edge_row_shortens_the_endpoint_in_its_own_file():
    edge = {
        "id": 1, "kind": "CALLS", "source": "scripts/run.ts::runMatrix",
        "target": "src/store.ts::Store.migrate", "file_path": "scripts/run.ts",
        "line": 512, "confidence": 1.0, "confidence_tier": "EXTRACTED",
    }
    assert (
        compact.edge_row(edge)
        == "CALLS | runMatrix -> src/store.ts::Store.migrate | scripts/run.ts:512"
    )


def test_edge_row_keeps_a_confidence_that_is_not_the_default():
    edge = {
        "kind": "CALLS", "source": "a.py::f", "target": "b.py::g",
        "file_path": "a.py", "line": 3, "confidence": 0.6,
        "confidence_tier": "INFERRED",
    }
    assert compact.edge_row(edge) == "CALLS | f -> b.py::g | a.py:3 | INFERRED"


def test_flow_and_community_rows_lead_with_the_id_their_drill_down_takes():
    flow = {
        "id": 1, "name": "constructor", "entry_point_id": 6599, "depth": 6,
        "node_count": 72, "file_count": 8, "criticality": 0.8906,
        "path": [6599, 6667, 6674], "created_at": "x", "updated_at": "x",
    }
    assert compact.flow_row(flow) == (
        "id 1 | constructor | criticality 0.8906 | 72 nodes, 8 files, depth 6"
    )
    community = {
        "id": 12, "name": "sync-real", "level": 0, "cohesion": 0.1435,
        "size": 6605, "dominant_language": "typescript",
        "members": ["a.ts::f"], "members_total": 6605,
    }
    assert compact.community_row(community) == (
        "id 12 | sync-real | 6605 nodes | cohesion 0.1435"
    )


def test_compact_drops_duplicates_nulls_and_hints():
    result = {
        "status": "ambiguous",
        "summary": "'f' matches 2 node(s).",
        "candidates": [{"kind": "Function", "qualified_name": "a.py::f",
                        "file_path": "a.py", "line_start": 1}],
        "disambiguation": [{"kind": "Function", "qualified_name": "a.py::f",
                            "file_path": "a.py", "line_start": 1}],
        "description": "Find all functions that call a given function",
        "confidence": None,
        "_hints": {"next_steps": [{"tool": "query_graph"}]},
    }
    out = compact.compact("query", result)
    # `disambiguation` is kept because the summary and hint name it; its
    # rows carry the qualified name those instructions say to pass back.
    assert out["disambiguation"] == ["Function | a.py::f | line 1"]
    assert "candidates" not in out
    assert "_hints" not in out
    assert "description" not in out
    assert "confidence" not in out
    # A status that is not "ok" says something.
    assert out["status"] == "ambiguous"


def test_ok_status_is_a_default_and_goes():
    out = compact.compact("search", {"status": "ok", "summary": "s", "results": []})
    assert out == {"summary": "s", "results": []}


def test_commands_without_a_spec_are_untouched():
    result = {"status": "ok", "summary": "s", "suggestions": [{"id": 1}]}
    assert compact.compact("refactor", dict(result)) == result


# --------------------------------------------------------------------------
# architecture: the overview in one call
# --------------------------------------------------------------------------

_MINIMAL_ARCHITECTURE = {
    "status": "ok",
    "summary": "Architecture: 2 communities, 1 community pairs, 1 warning(s)",
    "communities": [
        {"id": 12, "name": "sync-real", "size": 6605, "cohesion": 0.1435,
         "dominant_language": "typescript",
         "top_dirs": [["tests/worker", 21], ["tests", 14]]},
        {"id": 11, "name": "shared-server", "size": 3719, "cohesion": 0.2794,
         "dominant_language": "typescript", "top_dirs": [["src/services", 52]]},
    ],
    "cross_community_edges": [
        {"source_community": "shared-server", "target_community": "sync-real",
         "edge_count": 5024, "top_kinds": ["CALLS", "REFERENCES"],
         "high_coupling": True},
    ],
    "warnings": ["High coupling (5024 edges) between 'shared-server' and 'sync-real'"],
    "cross_community_edges_total": 1,
    "truncated": False,
    "_hints": {"next_steps": [{"tool": "list_communities"}]},
    "context_savings": {"estimated": True, "saved_tokens": 1475207, "saved_percent": 100},
}


def test_architecture_community_row_says_where_it_lives():
    assert compact.architecture_community_row(_MINIMAL_ARCHITECTURE["communities"][0]) == (
        "sync-real | 6605 nodes | typescript | tests/worker 21%, tests 14%"
    )


def test_architecture_connection_row_is_undirected_and_flags_coupling():
    # Pairs are aggregated in canonical order, so "source" and "target" are
    # not a direction; the row must not claim one.
    assert compact.connection_row(_MINIMAL_ARCHITECTURE["cross_community_edges"][0]) == (
        "shared-server <-> sync-real | 5024 edges | CALLS, REFERENCES | high coupling"
    )
    calm = dict(_MINIMAL_ARCHITECTURE["cross_community_edges"][0], high_coupling=False)
    assert compact.connection_row(calm).endswith("| CALLS, REFERENCES")


def test_compact_architecture_drops_what_rows_restate():
    import copy

    out = compact.compact("architecture", copy.deepcopy(_MINIMAL_ARCHITECTURE))
    assert out == {
        "summary": "Architecture: 2 communities, 1 community pairs, 1 warning(s)",
        "communities": [
            "sync-real | 6605 nodes | typescript | tests/worker 21%, tests 14%",
            "shared-server | 3719 nodes | typescript | src/services 52%",
        ],
        "cross_community_edges": [
            "shared-server <-> sync-real | 5024 edges | CALLS, REFERENCES | high coupling",
        ],
    }


def test_standard_architecture_rows_keep_the_members_asked_for():
    community = {"id": 1, "name": "a", "size": 3, "members": ["x.py::f"]}
    edge = {"source_community": 1, "target_community": 2, "edge_kind": "CALLS",
            "source": "x.py::f", "target": "y.py::g"}
    assert compact.architecture_community_row(community) == community
    assert compact.connection_row(edge) == edge
    out = compact.compact("architecture", {
        "summary": "s", "communities": [community], "cross_community_edges": [edge],
        "warnings": ["High coupling (12 edges) between 'a' and 'b'"],
    })
    # Per-edge rows carry no coupling flag, so the warnings stay.
    assert out["warnings"] == ["High coupling (12 edges) between 'a' and 'b'"]


# --------------------------------------------------------------------------
# One flag, everywhere compaction applies
# --------------------------------------------------------------------------


def _parser():
    captured = {}

    def fake_parse(self, *a, **k):
        captured["parser"] = self
        raise SystemExit(0)

    with patch.object(sys, "argv", ["cartograph", "status"]):
        with patch("argparse.ArgumentParser.parse_args", fake_parse):
            with pytest.raises(SystemExit):
                cli.main()
    return captured["parser"]


def test_every_compacted_command_takes_the_same_detail_flag():
    parser = _parser()
    sub = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    for name in compact.COMMANDS:
        detail = [a for a in sub.choices[name]._actions if a.dest == "detail"]
        assert detail, f"{name} has no --detail"
        assert detail[0].option_strings == ["--detail"]
        assert tuple(detail[0].choices) == ("compact", "full")
        assert detail[0].default == "compact"
    # And nowhere else: a flag that does nothing is a catalogue that lies.
    for name, command in sub.choices.items():
        if name not in compact.COMMANDS:
            assert not any(a.dest == "detail" for a in command._actions), name


# --------------------------------------------------------------------------
# End to end through the CLI
# --------------------------------------------------------------------------


def _run(argv, capsys):
    with patch.object(sys, "argv", ["cartograph", *argv]):
        with pytest.raises(SystemExit) as exc:
            cli.main()
    assert exc.value.code == 0
    return json.loads(capsys.readouterr().out)


def test_compacting_keeps_what_the_envelope_reads_from_data(tmp_path, monkeypatch, capsys):
    """`search_mode` and `truncated` leave `data` but must reach the envelope.

    Both are duplicates in `data` once the envelope carries them, and the
    `data` copy of search_mode is the store's own word ("fts"). Dropping them
    before the envelope read them would silently lose the degradation signal
    and the page_limit truncation.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "graph.db").touch()
    monkeypatch.setenv("CRG_DATA_DIR", str(data_dir))
    result = {
        "status": "ok", "query": "login", "search_mode": "fts",
        "summary": "Found 1 node(s) matching 'login'", "truncated": True,
        "results": [{
            "kind": "Function", "name": "login",
            "qualified_name": f"{repo}/auth.py::login",
            "file_path": f"{repo}/auth.py", "line_start": 3, "score": 0.5,
        }],
    }
    with patch("cartograph.tools.semantic_search_nodes", return_value=result):
        env = _run(["search", "login", "--repo", str(repo)], capsys)
    assert env["data"] == {
        "summary": "Found 1 node(s) matching 'login'",
        "results": ["Function | login | auth.py:3"],
        # Attached by the CLI; this graph's paths name no file on disk.
        "coverage": "no code files in the working tree",
    }
    assert env["search_mode"] == "keyword"
    assert env["truncated"] is True
    assert env["truncated_reason"] == "page_limit"
