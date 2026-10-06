"""An incremental update leaves flows and communities as a full rebuild would.

Nodes store absolute file paths, and a re-parsed file's nodes come back with
new ids. The incremental post-process once matched repo-relative paths against
the stored absolute ones, so it re-traced nothing: a new entry point got no
flow and no community, flows through a changed or deleted file kept members
that no longer existed. Each test here commits a change, runs ``update``, and
compares the result with a full rebuild of the same tree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cartograph.graph import GraphStore
from cartograph.tools.build import build_or_update_graph

_FILES = {
    "pkg/__init__.py": "",
    "pkg/app.py": (
        "from pkg.service import serve\n"
        "from pkg.extra import cron\n"
        "\n"
        "\n"
        "def main():\n"
        "    serve()\n"
        "    cron()\n"
    ),
    "pkg/service.py": (
        "from pkg.store import save\n"
        "from pkg.tools import orphan\n"
        "\n"
        "\n"
        "def serve():\n"
        "    save()\n"
        "    orphan()\n"
    ),
    "pkg/store.py": (
        "def save():\n"
        "    write()\n"
        "\n"
        "\n"
        "def write():\n"
        "    return 1\n"
    ),
    "pkg/extra.py": (
        "from pkg.store import save\n"
        "\n"
        "\n"
        "def cron():\n"
        "    save()\n"
        "    tick()\n"
        "\n"
        "\n"
        "def tick():\n"
        "    return 2\n"
    ),
    "pkg/tools.py": (
        "from pkg.store import write\n"
        "\n"
        "\n"
        "def orphan():\n"
        "    write()\n"
        "    detail()\n"
        "\n"
        "\n"
        "def detail():\n"
        "    return 3\n"
    ),
    "pkg/batch.py": (
        "def nightly():\n"
        "    compact()\n"
        "\n"
        "\n"
        "def compact():\n"
        "    return 4\n"
    ),
}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo, check=True, capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = (tmp_path / "repo").resolve()
    for rel, text in _FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    result = build_or_update_graph(full_rebuild=True, repo_root=str(root))
    assert result["status"] == "ok"
    return root


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def _flows(repo: Path) -> set[tuple[str, frozenset[str]]]:
    """Each flow as (entry point, members), by qualified name.

    A member or entry point whose node no longer exists is kept as a marker,
    so a flow holding a deleted node does not look like a clean one.
    """
    store = GraphStore(repo / ".cartograph" / "graph.db")
    try:
        conn = store._conn
        qn = {
            r[0]: r[1]
            for r in conn.execute("SELECT id, qualified_name FROM nodes")
        }
        out: set[tuple[str, frozenset[str]]] = set()
        for fid, ep in conn.execute("SELECT id, entry_point_id FROM flows"):
            members = frozenset(
                qn.get(r[0], "<missing node>")
                for r in conn.execute(
                    "SELECT node_id FROM flow_memberships WHERE flow_id = ?",
                    (fid,),
                )
            )
            out.add((qn.get(ep, "<missing entry point>"), members))
        return out
    finally:
        store.close()


def _community_mates(repo: Path, names: list[str]) -> dict[str, frozenset[str]]:
    """For each named function, the qualified names in its community."""
    store = GraphStore(repo / ".cartograph" / "graph.db")
    try:
        conn = store._conn
        out: dict[str, frozenset[str]] = {}
        for name in names:
            row = conn.execute(
                "SELECT community_id FROM nodes WHERE name = ? AND kind = 'Function'",
                (name,),
            ).fetchone()
            assert row is not None, name
            cid = row[0]
            if cid is None:
                out[name] = frozenset()
                continue
            out[name] = frozenset(
                r[0] for r in conn.execute(
                    "SELECT qualified_name FROM nodes WHERE community_id = ?",
                    (cid,),
                )
            )
        return out
    finally:
        store.close()


def _entry(flows: set[tuple[str, frozenset[str]]], name: str):
    return [f for f in flows if f[0].endswith(f"::{name}")]


def _update_then_rebuild(repo: Path, names: list[str]):
    result = build_or_update_graph(repo_root=str(repo))
    assert result["build_type"] == "incremental", result
    incremental = (_flows(repo), _community_mates(repo, names))
    build_or_update_graph(full_rebuild=True, repo_root=str(repo))
    full = (_flows(repo), _community_mates(repo, names))
    return incremental, full


def test_new_entry_point_gets_its_flow_and_a_community(repo: Path) -> None:
    path = repo / "pkg" / "service.py"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n\ndef newentry():\n    serve()\n    save()\n",
        encoding="utf-8",
    )
    _commit(repo, "newentry")

    (inc_flows, inc_comm), (full_flows, full_comm) = _update_then_rebuild(
        repo, ["newentry"],
    )

    assert len(_entry(full_flows, "newentry")) == 1
    assert inc_flows == full_flows
    assert full_comm["newentry"], "a full rebuild puts newentry in a community"
    assert inc_comm == full_comm


def test_modified_function_updates_flows_through_it(repo: Path) -> None:
    # write() in store.py now calls a new flush(); main() lives in app.py,
    # which did not change, and its flow runs through write().
    (repo / "pkg" / "store.py").write_text(
        "def save():\n"
        "    write()\n"
        "\n"
        "\n"
        "def write():\n"
        "    flush()\n"
        "    return 1\n"
        "\n"
        "\n"
        "def flush():\n"
        "    return 0\n",
        encoding="utf-8",
    )
    _commit(repo, "flush")

    (inc_flows, inc_comm), (full_flows, full_comm) = _update_then_rebuild(
        repo, ["flush", "write"],
    )

    main_flow = _entry(full_flows, "main")
    assert len(main_flow) == 1
    assert any(m.endswith("::flush") for m in main_flow[0][1])
    assert inc_flows == full_flows
    assert inc_comm == full_comm


def test_deleted_file_flows_are_removed_and_retraced(repo: Path) -> None:
    (repo / "pkg" / "extra.py").unlink()
    _commit(repo, "drop extra")

    (inc_flows, _), (full_flows, _) = _update_then_rebuild(repo, ["main"])

    assert not _entry(full_flows, "cron")
    main_flow = _entry(full_flows, "main")
    assert len(main_flow) == 1
    assert not any(m.endswith(("::cron", "::tick")) for m in main_flow[0][1])
    assert inc_flows == full_flows


def test_function_left_without_callers_becomes_an_entry_point(repo: Path) -> None:
    # serve() no longer calls orphan(); orphan() lives in tools.py, which did
    # not change, and is now a root with a flow of its own.
    (repo / "pkg" / "service.py").write_text(
        "from pkg.store import save\n"
        "\n"
        "\n"
        "def serve():\n"
        "    save()\n",
        encoding="utf-8",
    )
    _commit(repo, "orphan")

    (inc_flows, _), (full_flows, _) = _update_then_rebuild(repo, ["orphan"])

    assert len(_entry(full_flows, "orphan")) == 1
    assert inc_flows == full_flows


def test_unchanged_flows_keep_their_ids(repo: Path) -> None:
    """A flow nothing touched keeps the id an agent may already hold."""
    def ids() -> dict[str, int]:
        store = GraphStore(repo / ".cartograph" / "graph.db")
        try:
            return {
                r[1]: r[0]
                for r in store._conn.execute(
                    "SELECT f.id, n.qualified_name FROM flows f "
                    "JOIN nodes n ON n.id = f.entry_point_id"
                )
            }
        finally:
            store.close()

    before = ids()
    path = repo / "pkg" / "service.py"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n\ndef newentry():\n    serve()\n",
        encoding="utf-8",
    )
    _commit(repo, "newentry")
    build_or_update_graph(repo_root=str(repo))
    after = ids()

    nightly = next(q for q in before if q.endswith("::nightly"))
    assert after[nightly] == before[nightly]


def test_no_change_update_leaves_flows_and_communities_alone(repo: Path) -> None:
    before = (_flows(repo), _community_mates(repo, ["main", "nightly"]))
    result = build_or_update_graph(repo_root=str(repo))
    assert result["summary"].startswith("No changes detected")
    assert (_flows(repo), _community_mates(repo, ["main", "nightly"])) == before


@pytest.mark.parametrize("leiden", [False, True])
def test_communities_do_not_depend_on_row_order(
    tmp_path: Path, monkeypatch, leiden: bool,
) -> None:
    """Re-parsed nodes come back last in id order; communities must not care.

    Leiden numbers vertices in the order it is given them, so a graph read in
    id order after an update would partition differently from the same graph
    after a full rebuild.
    """
    import random

    from cartograph import communities
    from cartograph.parser import EdgeInfo, NodeInfo

    if leiden and not communities.IGRAPH_AVAILABLE:
        pytest.skip("igraph not installed")
    if not leiden:
        monkeypatch.setattr(communities, "IGRAPH_AVAILABLE", False)

    rng = random.Random(7)
    names = [f"m{i % 12}.py::f{i}" for i in range(240)]
    edges = set()
    for i, src in enumerate(names):
        for _ in range(3):
            j = i + rng.randint(-15, 15) if rng.random() < 0.8 else rng.randrange(240)
            if 0 <= j < 240 and j != i:
                edges.add((src, names[j]))

    def build(order: list[str]) -> GraphStore:
        store = GraphStore(tmp_path / f"{len(list(tmp_path.iterdir()))}.db")
        for qn in order:
            path, _, name = qn.partition("::")
            store.upsert_node(NodeInfo(
                kind="Function", name=name, file_path=path,
                line_start=1, line_end=2, language="python",
                parent_name=None,
            ))
        for src, tgt in sorted(edges, key=lambda e: order.index(e[0])):
            store.upsert_edge(EdgeInfo(
                kind="CALLS", source=src, target=tgt,
                file_path=src.partition("::")[0], line=1,
            ))
        store.commit()
        return store

    def partition(store: GraphStore) -> set[frozenset[str]]:
        try:
            return {
                frozenset(c["members"])
                for c in communities.detect_communities(store)
            }
        finally:
            store.close()

    shuffled = list(names)
    rng.shuffle(shuffled)
    assert partition(build(names)) == partition(build(shuffled))
