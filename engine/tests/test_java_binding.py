"""Java call binding: receiver, class hierarchy and declared type.

Each test builds a small repository end to end (parse, store, post-process)
and asks what an agent asks: who calls this method, and what does a change to
this file affect. A wrong answer here is an invented or a dropped caller.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from cartograph.graph import GraphStore
from cartograph.incremental import full_build
from cartograph.postprocessing import run_post_processing
from cartograph.tools.query import query_graph



def _build(tmp_path: Path, files: dict[str, str]) -> GraphStore:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".cartograph").mkdir()
    store = GraphStore(tmp_path / ".cartograph" / "graph.db")
    with patch(
        "cartograph.incremental.get_all_tracked_files",
        return_value=sorted(files),
    ):
        full_build(tmp_path, store)
    run_post_processing(store)
    return store


def _callers(tmp_path: Path, target: str) -> dict[str, str | None]:
    """``{caller short name: target_resolution}`` for ``callers_of``."""
    result = query_graph(
        pattern="callers_of",
        target=f"{(tmp_path / target).as_posix()}",
        repo_root=str(tmp_path),
    )
    return {
        row["qualified_name"].rsplit("::", 1)[-1]: row.get("target_resolution")
        for row in result["results"]
    }


def _impacted(store: GraphStore, tmp_path: Path, rel: str) -> set[str]:
    result = store.get_impact_radius([(tmp_path / rel).as_posix()])
    return {n.qualified_name.rsplit("::", 1)[-1] for n in result["impacted_nodes"]}


INTERFACE_FILES = {
    "src/app/Repo.java": (
        "package app;\n"
        "public interface Repo { void save(String s); }\n"
    ),
    "src/app/DbRepo.java": (
        "package app;\n"
        "public class DbRepo implements Repo {\n"
        "    public void save(String s) { log(s); }\n"
        "    void log(String s) { System.out.println(s); }\n"
        "}\n"
    ),
    "src/app/Other.java": (
        "package app;\n"
        "public class Other { public void save(String s) { } }\n"
    ),
}


def _service(handle_body: str) -> str:
    return (
        "package app;\n"
        "public class Service {\n"
        "    private final Repo repo;\n"
        "    private final Other other = new Other();\n"
        "    public Service(Repo repo) { this.repo = repo; }\n"
        "    public void handle() {\n"
        f"{handle_body}"
        "    }\n"
        "}\n"
    )


SEPARATE_LINES = '        repo.save("x");\n        other.save("y");\n'
ONE_LINE = '        repo.save("x"); other.save("y");\n'


@pytest.fixture(params=["sql", "networkx"])
def impact_engine(request, monkeypatch):
    # Both traversals start from the supertype methods an implementation
    # overrides; the engine is read once at import, so patch the constant.
    monkeypatch.setattr("cartograph.graph.BFS_ENGINE", request.param)
    return request.param


class TestInterfaceDispatch:
    def test_interface_call_reaches_implementation_via_supertype(self, tmp_path):
        store = _build(tmp_path, {
            **INTERFACE_FILES, "src/app/Service.java": _service(SEPARATE_LINES),
        })
        try:
            assert _callers(tmp_path, "src/app/DbRepo.java::DbRepo.save") == {
                "Service.handle": "via_supertype",
            }
            # The declared type binds the call itself to the interface.
            assert _callers(tmp_path, "src/app/Repo.java::Repo.save") == {
                "Service.handle": None,
            }
        finally:
            store.close()

    def test_impact_on_implementation_lists_interface_caller(
        self, tmp_path, impact_engine,
    ):
        store = _build(tmp_path, {
            **INTERFACE_FILES, "src/app/Service.java": _service(SEPARATE_LINES),
        })
        try:
            assert "Service.handle" in _impacted(store, tmp_path, "src/app/DbRepo.java")
        finally:
            store.close()


class TestSameLineReceivers:
    """Two same-named calls on one line, on receivers of different types."""

    def test_both_calls_are_stored(self, tmp_path):
        store = _build(tmp_path, {
            **INTERFACE_FILES, "src/app/Service.java": _service(ONE_LINE),
        })
        try:
            rows = store._conn.execute(
                "SELECT target_qualified, extra FROM edges "
                "WHERE kind = 'CALLS' AND source_qualified LIKE '%Service.handle' "
                "ORDER BY target_qualified"
            ).fetchall()
            assert [r["target_qualified"].rsplit("::", 1)[-1] for r in rows] == [
                "Other.save", "Repo.save",
            ]
        finally:
            store.close()

    def test_each_receiver_binds_to_its_own_type(self, tmp_path):
        store = _build(tmp_path, {
            **INTERFACE_FILES, "src/app/Service.java": _service(ONE_LINE),
        })
        try:
            assert _callers(tmp_path, "src/app/Other.java::Other.save") == {
                "Service.handle": None,
            }
            assert _callers(tmp_path, "src/app/Repo.java::Repo.save") == {
                "Service.handle": None,
            }
            assert _callers(tmp_path, "src/app/DbRepo.java::DbRepo.save") == {
                "Service.handle": "via_supertype",
            }
        finally:
            store.close()

    def test_impact_matches_the_separate_line_case(self, tmp_path, impact_engine):
        store = _build(tmp_path, {
            **INTERFACE_FILES, "src/app/Service.java": _service(ONE_LINE),
        })
        try:
            assert "Service.handle" in _impacted(store, tmp_path, "src/app/DbRepo.java")
        finally:
            store.close()

    def test_same_receiver_twice_on_a_line_is_one_call_site(self, tmp_path):
        store = _build(tmp_path, {
            **INTERFACE_FILES,
            "src/app/Service.java": _service('        repo.save("x"); repo.save("y");\n'),
        })
        try:
            count = store._conn.execute(
                "SELECT COUNT(*) FROM edges WHERE kind = 'CALLS' "
                "AND source_qualified LIKE '%Service.handle'"
            ).fetchone()[0]
            assert count == 1
        finally:
            store.close()


SUPER_FILES = {
    "src/app/Base.java": (
        "package app;\n"
        "public class Base { public void run() { } }\n"
    ),
    "src/app/Child.java": (
        "package app;\n"
        "public class Child extends Base {\n"
        "    @Override public void run() { super.run(); }\n"
        "}\n"
    ),
}


class TestSuperCall:
    def test_super_call_is_not_a_self_call(self, tmp_path):
        store = _build(tmp_path, SUPER_FILES)
        try:
            assert "Child.run" not in _callers(tmp_path, "src/app/Child.java::Child.run")
        finally:
            store.close()

    def test_super_call_is_a_caller_of_the_parent_method(self, tmp_path):
        store = _build(tmp_path, SUPER_FILES)
        try:
            assert _callers(tmp_path, "src/app/Base.java::Base.run") == {
                "Child.run": None,
            }
        finally:
            store.close()


class TestDeclaredType:
    def test_typed_receiver_binds_to_declared_type_not_a_namesake(self, tmp_path):
        store = _build(tmp_path, {
            "src/app/Printer.java": (
                "package app;\n"
                "public class Printer { public void print(String s) { } }\n"
            ),
            "src/app/Report.java": (
                "package app;\n"
                "public class Report {\n"
                "    public void print(String s) { }\n"
                "    public void render(Printer p) { p.print(\"r\"); }\n"
                "}\n"
            ),
        })
        try:
            assert _callers(tmp_path, "src/app/Printer.java::Printer.print") == {
                "Report.render": None,
            }
            assert _callers(tmp_path, "src/app/Report.java::Report.print") == {}
        finally:
            store.close()


class TestPythonUnchanged:
    """The Java receiver rules must not move Python ``self.x()`` / ``obj.x()`` callers."""

    FILES = {
        "pkg/__init__.py": "",
        "pkg/store.py": (
            "class Store:\n"
            "    def save(self):\n"
            "        return 1\n"
            "\n"
            "    def flush(self):\n"
            "        return self.save()\n"
            "\n"
            "    def mirror(self, other):\n"
            "        return other.save()\n"
        ),
        "pkg/use.py": (
            "from pkg.store import Store\n"
            "\n"
            "def persist(obj):\n"
            "    return obj.save()\n"
            "\n"
            "def make():\n"
            "    s = Store()\n"
            "    return s.save()\n"
            "\n"
            "def both(a, b):\n"
            "    return a.save() + b.save()\n"
        ),
    }

    def test_self_and_object_calls_keep_their_callers(self, tmp_path):
        store = _build(tmp_path, self.FILES)
        try:
            callers = _callers(tmp_path, "pkg/store.py::Store.save")
            # As on 0.9.4: resolved by self and by import evidence.
            for name in ("Store.flush", "persist", "make", "both"):
                assert callers.pop(name) is None, name
            # A call on another object inside the class is no longer taken as
            # proof of the class's own method, but it is still listed.
            assert callers == {"Store.mirror": "unresolved"}
        finally:
            store.close()

    def test_two_receivers_on_one_line_are_two_calls(self, tmp_path):
        store = _build(tmp_path, self.FILES)
        try:
            receivers = sorted(
                r[0] for r in store._conn.execute(
                    "SELECT json_extract(extra, '$.receiver') FROM edges "
                    "WHERE kind = 'CALLS' AND source_qualified LIKE '%::both'"
                )
            )
            assert receivers == ["a", "b"]
        finally:
            store.close()

    def test_ruling_out_the_own_class_does_not_resolve_to_the_other(self, tmp_path):
        # `self._thread.start()` is not Daemon.start, and nothing says it is
        # Watcher.start either: that one stays a labelled candidate.
        store = _build(tmp_path, {
            "pkg/__init__.py": "",
            "pkg/daemon.py": (
                "class Watcher:\n"
                "    def start(self):\n"
                "        return 1\n"
                "\n"
                "class Daemon:\n"
                "    def start(self):\n"
                "        return 2\n"
                "\n"
                "    def start_health(self):\n"
                "        self._thread.start()\n"
            ),
        })
        try:
            assert _callers(tmp_path, "pkg/daemon.py::Watcher.start") == {
                "Daemon.start_health": "ambiguous",
            }
            assert _callers(tmp_path, "pkg/daemon.py::Daemon.start") == {
                "Daemon.start_health": "ambiguous",
            }
        finally:
            store.close()
