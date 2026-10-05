"""`impact` is short by default, and never hides how far a change reaches.

Measured on a 991-file repository, `carto impact --files SessionStore.ts`
answered with ~506,000 characters: 500 nodes and every connecting edge. The
default is now the most affected items, ranked, with everything else counted.
A short list is only safe if an agent cannot mistake it for the whole answer —
the failure being guarded is an agent that changes a signature, reads twenty
rows, and breaks a caller it was never shown. So each test here pins one part
of that promise against a graph whose answer is known in advance:

* the totals and the file set of the default response equal those of the
  complete response (`--detail full --limit <total>`);
* truncation is flagged exactly when items are left out;
* direct dependents beyond the limit are counted and announced;
* the order is a stated rule — direct first — and is the same every time, and
  on both traversal engines.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from unittest.mock import patch

import pytest

from cartograph import cli
from cartograph import graph as graph_module
from cartograph.graph import GraphStore
from cartograph.incremental import get_db_path
from cartograph.parser import EdgeInfo, NodeInfo

#: The graph below, counted by hand rather than by the code under test.
DIRECT_CALLERS = 25
UPSTREAM = 25                       # one transitive caller per direct caller
TOTAL_ITEMS = DIRECT_CALLERS + UPSTREAM + 1   # + importer.py, a direct importer
TOTAL_DIRECT = DIRECT_CALLERS + 1
CALLER_FILES = 13                   # callers/c0.py .. c12.py, two callers each but the last
TOTAL_FILES = CALLER_FILES + UPSTREAM + 1
TOTAL_EDGES = {"CALLS": DIRECT_CALLERS + UPSTREAM, "IMPORTS_FROM": 1}


@pytest.fixture()
def fan_in_repo(tmp_path, monkeypatch):
    """One changed function with 25 direct callers, each with a caller of its own.

    ``importer.py`` only imports the changed file. Its score (IMPORTS_FROM,
    0.5 x 0.6 = 0.3) is *below* that of every two-hop caller (CALLS twice,
    (1.0 x 0.6)^2 = 0.36), which is what makes "direct first" observable: a
    ranking by score alone would put it after all 25 transitive callers.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.chdir(repo)
    store = GraphStore(get_db_path(repo))

    def fn(path: str, name: str, line: int) -> str:
        store.upsert_node(NodeInfo(
            kind="Function", name=name, file_path=path, line_start=line,
            line_end=line + 3, language="python",
        ))
        return f"{path}::{name}"

    def file_node(path: str) -> str:
        store.upsert_node(NodeInfo(
            kind="File", name=path, file_path=path, line_start=1, line_end=40,
            language="python",
        ))
        return path

    def edge(kind: str, source: str, target: str, path: str, line: int) -> None:
        store.upsert_edge(EdgeInfo(
            kind=kind, source=source, target=target, file_path=path, line=line,
        ))

    core = str(repo / "core.py")
    file_node(core)
    target = fn(core, "target", 10)
    for i in range(DIRECT_CALLERS):
        caller_path = str(repo / "callers" / f"c{i // 2}.py")
        caller = fn(caller_path, f"call{i}", 5 + 10 * (i % 2))
        edge("CALLS", caller, target, caller_path, 6 + 10 * (i % 2))
        # "api/" sorts before "callers/", so an order by name alone would put
        # every transitive caller first and could not pass for the ranking.
        up_path = str(repo / "api" / f"u{i}.py")
        up = fn(up_path, f"up{i}", 3)
        edge("CALLS", up, caller, up_path, 4)
    importer = file_node(str(repo / "importer.py"))
    edge("IMPORTS_FROM", importer, core, importer, 1)
    store.commit()
    store.close()
    return repo


def _run(repo, *extra, capsys):
    argv = ["cartograph", "impact", "--files", "core.py", "--repo", str(repo), *extra]
    with patch.object(sys, "argv", argv):
        with pytest.raises(SystemExit) as exc:
            cli.main()
    assert exc.value.code == 0
    return json.loads(capsys.readouterr().out)


def _row_file(row: str) -> str:
    """The path field of a compact item row: ``<direct|transitive> | <kind> | <name> | <path>:<line>``."""
    return row.rsplit(" | ", 1)[-1].rsplit(":", 1)[0]


def _file_rows(data) -> dict[str, tuple[int, int]]:
    """``<path> | <n> items (<d> direct)`` rows, as {path: (items, direct)}."""
    out = {}
    for row in data["affected_files"]:
        path, counts = row.split(" | ")
        items = int(counts.split(" item")[0])
        direct = int(counts.split("(")[1].split(" direct")[0]) if "direct" in counts else 0
        out[path] = (items, direct)
    return out


def _complete(repo, capsys):
    """Everything: every item, whole rows, every edge."""
    return _run(repo, "--detail", "full", "--limit", "1000", capsys=capsys)


# --------------------------------------------------------------------------
# Totals and the file set: the default says the whole scope
# --------------------------------------------------------------------------


def test_default_totals_equal_the_complete_response(fan_in_repo, capsys):
    default = _run(fan_in_repo, capsys=capsys)["data"]
    full = _complete(fan_in_repo, capsys)["data"]

    items = full["impacted_nodes"]
    assert len(items) == TOTAL_ITEMS            # the complete response is complete
    totals = default["totals"]
    assert totals["items"] == len(items) == TOTAL_ITEMS
    assert totals["direct"] == sum(1 for n in items if n["direct"]) == TOTAL_DIRECT
    assert totals["files"] == len({n["file_path"] for n in items}) == TOTAL_FILES
    assert totals["edges"] == dict(Counter(e["kind"] for e in full["edges"])) == TOTAL_EDGES


def test_default_lists_every_affected_file_with_its_counts(fan_in_repo, capsys):
    """The completeness guarantee: short on items, never short on files."""
    default = _run(fan_in_repo, capsys=capsys)["data"]
    full = _complete(fan_in_repo, capsys)["data"]

    assert len(default["impacted_nodes"]) == 20
    per_file = Counter(n["file_path"] for n in full["impacted_nodes"])
    direct_per_file = Counter(n["file_path"] for n in full["impacted_nodes"] if n["direct"])
    expected = {f: (per_file[f], direct_per_file[f]) for f in per_file}
    assert _file_rows(default) == expected
    assert len(expected) == TOTAL_FILES
    # And the complete response's own file list says the same set.
    assert set(full["impacted_files"]) == set(expected)


def test_file_list_does_not_depend_on_the_limit(fan_in_repo, capsys):
    one = _run(fan_in_repo, "--limit", "1", capsys=capsys)["data"]
    assert len(one["impacted_nodes"]) == 1
    assert len(one["affected_files"]) == TOTAL_FILES
    assert one["totals"]["items"] == TOTAL_ITEMS


# --------------------------------------------------------------------------
# Truncation: flagged exactly when something is left out
# --------------------------------------------------------------------------


def test_not_truncated_when_every_item_is_shown(fan_in_repo, capsys):
    env = _run(fan_in_repo, "--limit", str(TOTAL_ITEMS), capsys=capsys)
    assert len(env["data"]["impacted_nodes"]) == TOTAL_ITEMS
    assert env["truncated"] is False
    assert "see_all" not in env["data"]
    # A full page is not evidence of more when the total is known.
    assert env["page"]["has_more"] is False
    assert "all shown" in env["data"]["summary"]


def test_truncated_when_one_item_is_left_out(fan_in_repo, capsys):
    env = _run(fan_in_repo, "--limit", str(TOTAL_ITEMS - 1), capsys=capsys)
    assert env["truncated"] is True
    assert env["truncated_reason"] == "page_limit"
    assert env["page"]["has_more"] is True
    assert env["page"]["total_estimated"] == TOTAL_ITEMS
    see_all = env["data"]["see_all"]
    assert see_all.startswith("carto impact --files core.py ")
    assert f"--limit {TOTAL_ITEMS}" in see_all
    assert "--detail full" in see_all


def test_the_see_all_command_shows_everything(fan_in_repo, capsys):
    """The remediation is a command, and running it must give the whole answer."""
    see_all = _run(fan_in_repo, capsys=capsys)["data"]["see_all"]
    argv = see_all.split()[2:]      # drop "carto impact"
    with patch.object(sys, "argv", ["cartograph", "impact", *argv]):
        with pytest.raises(SystemExit):
            cli.main()
    env = json.loads(capsys.readouterr().out)
    assert env["truncated"] is False
    assert len(env["data"]["impacted_nodes"]) == TOTAL_ITEMS
    assert len(env["data"]["edges"]) == sum(TOTAL_EDGES.values())


# --------------------------------------------------------------------------
# Direct dependents: never silently dropped
# --------------------------------------------------------------------------


def test_direct_dependents_beyond_the_limit_are_counted_and_announced(fan_in_repo, capsys):
    data = _run(fan_in_repo, capsys=capsys)["data"]
    assert data["totals"]["direct"] == TOTAL_DIRECT
    assert all(row.startswith("direct | ") for row in data["impacted_nodes"])
    summary = data["summary"]
    assert "\n" not in summary
    assert f"{TOTAL_DIRECT} direct dependents; 20 shown" in summary
    # The way to list exactly the direct set: they rank first.
    assert f"--limit {TOTAL_DIRECT}" in summary


def test_direct_dependents_rank_before_higher_scoring_transitive_ones(fan_in_repo, capsys):
    """`--limit <direct total>` returns exactly the direct set.

    importer.py scores below every two-hop caller, so under a ranking by score
    alone it would be the last direct item, after 25 transitive ones.
    """
    data = _run(fan_in_repo, "--limit", str(TOTAL_DIRECT), capsys=capsys)["data"]
    rows = data["impacted_nodes"]
    assert all(row.startswith("direct | ") for row in rows)
    assert {_row_file(r) for r in rows} == (
        {f"callers/c{i}.py" for i in range(CALLER_FILES)} | {"importer.py"}
    )
    after = _run(fan_in_repo, "--limit", str(TOTAL_DIRECT + 1), capsys=capsys)["data"]
    assert after["impacted_nodes"][-1].startswith("transitive | ")


def test_summary_states_the_scope_on_one_line(fan_in_repo, capsys):
    summary = _run(fan_in_repo, capsys=capsys)["data"]["summary"]
    assert summary.startswith(
        f"core.py: {TOTAL_ITEMS} items affected within 2 hops across "
        f"{TOTAL_FILES} files ({TOTAL_DIRECT} direct: {DIRECT_CALLERS} call, "
        "1 import only); showing top 20"
    )


# --------------------------------------------------------------------------
# Ranking is deterministic
# --------------------------------------------------------------------------


def test_ranking_is_the_same_every_time(fan_in_repo, capsys):
    first = _run(fan_in_repo, "--limit", "1000", capsys=capsys)["data"]
    second = _run(fan_in_repo, "--limit", "1000", capsys=capsys)["data"]
    assert first["impacted_nodes"] == second["impacted_nodes"]
    assert first["affected_files"] == second["affected_files"]


def test_equal_scores_are_ordered_by_name(fan_in_repo, capsys):
    """The 25 direct callers all score 0.6; only the name can order them."""
    full = _run(fan_in_repo, "--detail", "full", "--limit", str(DIRECT_CALLERS),
                capsys=capsys)["data"]
    names = [n["qualified_name"] for n in full["impacted_nodes"]]
    assert len({n["impact_score"] for n in full["impacted_nodes"]}) == 1
    assert names == sorted(names)


def test_both_traversal_engines_agree(fan_in_repo, capsys, monkeypatch):
    sql = _complete(fan_in_repo, capsys)["data"]
    monkeypatch.setattr(graph_module, "BFS_ENGINE", "networkx")
    nx = _complete(fan_in_repo, capsys)["data"]
    assert [n["qualified_name"] for n in nx["impacted_nodes"]] == [
        n["qualified_name"] for n in sql["impacted_nodes"]
    ]
    assert nx["totals"] == sql["totals"]
    assert nx["impacted_files"] == sql["impacted_files"]


# --------------------------------------------------------------------------
# Shape: compact by default, today's shape on --detail full
# --------------------------------------------------------------------------


def test_default_carries_counts_not_edges(fan_in_repo, capsys):
    data = _run(fan_in_repo, capsys=capsys)["data"]
    assert "edges" not in data
    assert "changed_nodes" not in data
    # `affected_files` says the same paths, with counts.
    assert "impacted_files" not in data
    assert data["totals"]["changed_nodes"] == 2
    assert data["changed_files"] == ["core.py"]


def test_detail_full_keeps_every_edge_and_whole_rows(fan_in_repo, capsys):
    data = _complete(fan_in_repo, capsys)["data"]
    assert len(data["edges"]) == sum(TOTAL_EDGES.values())
    assert all(isinstance(n, dict) for n in data["impacted_nodes"])
    assert len(data["changed_nodes"]) == 2
    assert all(isinstance(f, str) for f in data["impacted_files"])
