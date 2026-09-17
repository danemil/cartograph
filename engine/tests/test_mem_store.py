"""The memory store: schema, filters, and the honesty of `search_mode`.

The mode assertions are the load-bearing ones. Everything else here is
ordinary storage behaviour; reporting keyword hits as semantic is the single
failure the capability contract was written to prevent, so every route into the
vector path has a test that it degrades loudly rather than quietly.
"""

from __future__ import annotations

import sqlite3

import pytest
from cartograph import envelope
from cartograph.mem import store as mem_store
from cartograph.mem import vector as mem_vector


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    with mem_store.MemoryStore(tmp_path / "memory.db") as opened:
        yield opened


def _add(store, title, body="", **kwargs):
    kwargs.setdefault("project", "demo")
    return store.add(title=title, body=body, **kwargs)


class TestSchema:
    def test_migrations_stamp_the_version(self, store):
        assert store._conn.execute("PRAGMA user_version").fetchone()[0] == (
            mem_store.LATEST_VERSION
        )

    def test_reopening_is_idempotent(self, tmp_path):
        path = tmp_path / "memory.db"
        with mem_store.MemoryStore(path) as first:
            _add(first, "kept across reopen")
        with mem_store.MemoryStore(path) as second:
            items, _, _ = second.search(query="reopen")
        assert [item["title"] for item in items] == ["kept across reopen"]

    def test_fts_index_follows_the_table(self, store):
        """The triggers, not a rebuild, are what keep the index true.

        A stale index returns rowids whose rows are gone; the join drops them
        and the search under-reports with nothing saying so.
        """
        record = _add(store, "transient observation", "body text")
        store._conn.execute("DELETE FROM observations WHERE id = ?", (record["id"],))
        store._conn.commit()
        assert store.search(query="transient")[0] == []

        _add(store, "renamed later", "body text")
        store._conn.execute("UPDATE observations SET title = 'renamed now'")
        store._conn.commit()
        items, _, _ = store.search(query="renamed now")
        assert [item["title"] for item in items] == ["renamed now"]


class TestWrites:
    def test_created_at_strictly_increases(self, store):
        """Cursors bind to the newest timestamp, so it must move on every write.

        Two rows sharing a timestamp would let a cursor minted before a write
        survive it, which is the spliced-page failure the binding exists to
        prevent.
        """
        stamps = [_add(store, f"note {i}")["created_at"] for i in range(25)]
        assert stamps == sorted(stamps)
        assert len(set(stamps)) == len(stamps)

    def test_provenance_moves_with_the_store(self, store):
        before = store.provenance()
        _add(store, "something new")
        after = store.provenance()
        assert after["built_at"] != before["built_at"]
        assert after["observations"] == before["observations"] + 1
        # The memory answers no question about the graph, and saying otherwise
        # would make two commands' provenance blocks mean different things.
        assert after["graph_sha"] is None

    def test_identical_text_stays_two_observations(self, store):
        first = _add(store, "same title", "same body")
        second = _add(store, "same title", "same body")
        assert first["id"] != second["id"]


class TestSearch:
    def test_empty_result_is_a_success_not_an_error(self, store):
        _add(store, "something")
        items, mode, _ = store.search(query="nothing_matches_this")
        assert items == []
        assert mode == "fts"

    @pytest.mark.parametrize(
        "query",
        ['a "quoted phrase', "dashes - and * stars", "NEAR(a b)", "col:umn", "((("],
    )
    def test_fts_syntax_in_a_question_is_not_syntax(self, store, query):
        """An agent's prose must never be read as FTS5 syntax.

        Unescaped, each of these raises OperationalError, which would reach the
        agent as an internal error for a perfectly well-formed question.
        """
        _add(store, "quoted phrase", "dashes and stars and columns")
        items, _, _ = store.search(query=query)
        assert isinstance(items, list)

    def test_filters(self, store):
        _add(store, "alpha one", "shared word", kind="decision", session="s1",
             platform_source="copilot")
        _add(store, "alpha two", "shared word", kind="note", session="s2",
             platform_source="claude-code", doc_type="prompts")
        _add(store, "alpha three", "shared word", project="other", kind="note")

        def titles(**kwargs):
            return sorted(i["title"] for i in store.search(query="shared", **kwargs)[0])

        assert titles() == ["alpha one", "alpha three", "alpha two"]
        assert titles(project="other") == ["alpha three"]
        assert titles(kinds=["decision"]) == ["alpha one"]
        assert titles(kinds=["decision", "note"]) == [
            "alpha one", "alpha three", "alpha two",
        ]
        assert titles(session="s2") == ["alpha two"]
        assert titles(doc_type="prompts") == ["alpha two"]
        assert titles(platform_source="copilot") == ["alpha one"]

    def test_date_end_includes_the_day_it_names(self, store):
        record = _add(store, "dated observation", "body")
        day = record["created_at"][:10]
        # Compared as text, a bare date sorts before every timestamp on that
        # day, so the obvious call would silently exclude what it asked for.
        assert store.search(query="dated", date_end=day)[0]
        assert store.search(query="dated", date_start=day)[0]
        assert store.search(query="dated", date_end="2000-01-01")[0] == []

    def test_date_order(self, store):
        for i in range(3):
            _add(store, f"ordered {i}", "shared word")
        ascending = [i["title"] for i in store.search(query="shared", order_by="date_asc")[0]]
        descending = [i["title"] for i in store.search(query="shared", order_by="date_desc")[0]]
        assert ascending == ["ordered 0", "ordered 1", "ordered 2"]
        assert descending == list(reversed(ascending))

    def test_limit_bounds_the_answer(self, store):
        for i in range(10):
            _add(store, f"bounded {i}", "shared word")
        assert len(store.search(query="shared", limit=4)[0]) == 4


class _FakeProvider:
    """Deterministic embeddings, so the vector path is testable without a model.

    Eight dimensions of word-hash counts. Nothing about the quality of the
    space matters here — only that identical text embeds identically and
    different text does not, which is enough to tell a working index from a
    missing one.
    """

    name = "fake:test"
    dimension = 8

    def embed_query(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for word in text.lower().split():
            vector[hash(word) % self.dimension] += 1.0
        norm = sum(value * value for value in vector) ** 0.5
        return [value / norm for value in vector] if norm else vector


class TestSearchModeNeverLies:
    """Every route into the vector path, and what it reports when it fails."""

    def test_without_sqlite_vec(self, store, monkeypatch):
        monkeypatch.setattr(mem_vector, "load", lambda conn: False)
        _add(store, "degraded", "body")
        assert store.search(query="degraded")[1] == "fts"
        available, reason = store.semantic_status()
        assert available is False
        assert "sqlite-vec" in reason

    def test_without_a_provider(self, store, monkeypatch):
        monkeypatch.setattr(mem_vector, "load", lambda conn: True)
        monkeypatch.setattr("cartograph.embeddings.get_provider", lambda *a, **k: None)
        _add(store, "degraded", "body")
        assert store.search(query="degraded")[1] == "fts"
        assert "provider" in store.semantic_status()[1]

    def test_when_the_provider_is_installed_but_unusable(self, store, monkeypatch):
        class _Broken:
            name = "broken"

            @property
            def dimension(self):
                raise RuntimeError("model not downloaded")

        monkeypatch.setattr(mem_vector, "load", lambda conn: True)
        monkeypatch.setattr("cartograph.embeddings.get_provider", lambda *a, **k: _Broken())
        _add(store, "degraded", "body")
        assert store.search(query="degraded")[1] == "fts"

    def test_when_the_index_answers_with_an_error(self, store, monkeypatch):
        monkeypatch.setattr(mem_vector, "load", lambda conn: True)
        monkeypatch.setattr("cartograph.embeddings.get_provider", lambda *a, **k: _FakeProvider())
        monkeypatch.setattr(mem_vector, "ensure_table", lambda conn, dim: None)
        monkeypatch.setattr(mem_vector, "upsert", lambda conn, rowid, vec: None)
        monkeypatch.setattr(mem_vector, "count", lambda conn: 5)
        # None is "the index did not answer", which must degrade; an empty list
        # would be a semantic answer that found nothing.
        monkeypatch.setattr(mem_vector, "knn", lambda conn, vec, limit: None)
        _add(store, "degraded", "body")
        assert store.search(query="degraded")[1] == "fts"

    def test_the_envelope_normalises_fts_to_keyword(self, store):
        """The store's own vocabulary must not reach an agent unnormalised."""
        _, mode, _ = store.search(query="anything")
        assert envelope.ok("mem search", search_mode=mode)["search_mode"] == "keyword"


class TestVectorPath:
    """The vector path itself, which only runs where sqlite-vec is installed."""

    @pytest.fixture
    def vec_store(self, store, monkeypatch):
        pytest.importorskip("sqlite_vec", reason="the vector path is optional by design")
        monkeypatch.setattr(
            "cartograph.embeddings.get_provider", lambda *a, **k: _FakeProvider()
        )
        return store

    def test_embeddings_are_written_and_searched(self, vec_store):
        assert _add(vec_store, "alpha beta", "gamma delta")["embedded"] is True
        items, mode, _ = vec_store.search(query="alpha beta")
        assert mode == "hybrid"
        assert [item["title"] for item in items] == ["alpha beta"]
        assert vec_store.semantic_status() == (True, None)

    def test_a_query_with_no_searchable_words_is_still_semantic(self, vec_store):
        _add(vec_store, "alpha beta", "gamma delta")
        # "..." reduces to no FTS tokens, so only the index can answer — and
        # "semantic" is then the truthful report, not an overclaim.
        assert vec_store.search(query="...")[1] == "semantic"

    def test_a_foreign_index_is_refused_rather_than_mixed(self, vec_store, monkeypatch):
        _add(vec_store, "alpha beta", "gamma delta")

        class _Wider(_FakeProvider):
            name = "fake:wider"
            dimension = 16

        monkeypatch.setattr(
            "cartograph.embeddings.get_provider", lambda *a, **k: _Wider()
        )
        with mem_store.MemoryStore(vec_store.path) as reopened:
            available, reason = reopened.semantic_status()
            assert available is False
            assert "dimensions" in reason
            assert reopened.search(query="alpha")[1] == "fts"


class TestVectorModule:
    def test_load_is_false_when_sqlite_vec_is_absent(self, monkeypatch):
        """Absence is the expected state, not an error state."""
        import builtins

        real_import = builtins.__import__

        def refuse(name, *args, **kwargs):
            if name == "sqlite_vec":
                raise ImportError("no sqlite_vec")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", refuse)
        assert mem_vector.load(sqlite3.connect(":memory:")) is False

    def test_count_of_a_missing_index_is_zero(self):
        assert mem_vector.count(sqlite3.connect(":memory:")) == 0


class TestRelaxedMatching:
    """FTS5 requires every term; an agent's question always has spare ones."""

    @pytest.fixture
    def seeded(self, store):
        _add(store, "Chose offset paging over keyset",
             "No pageable command guarantees a stable total sort key.")
        _add(store, "Dropped FalkorDB", "SSPLv1 and it needs a daemon.")
        return store

    def test_all_terms_present_is_not_relaxed(self, seeded):
        items, _, relaxed = seeded.search(query="offset paging")
        assert len(items) == 1
        assert relaxed is False

    def test_a_question_still_finds_the_note(self, seeded):
        # The words that make it a question — why, did, we — are in no record.
        # Requiring them is what used to turn this into no answer at all.
        items, _, relaxed = seeded.search(query="why did we choose offset paging")
        assert [i["title"] for i in items] == ["Chose offset paging over keyset"]
        assert relaxed is True

    def test_stopwords_alone_do_not_produce_a_hit(self, seeded):
        # "and" appears in the FalkorDB body. Returning it for a query whose
        # only shared word is "and" is luck, and an agent cannot tell luck from
        # retrieval — so the honest answer is nothing.
        items, _, relaxed = seeded.search(query="what database did we reject and why")
        assert items == []
        assert relaxed is False

    def test_a_genuinely_absent_term_still_misses(self, seeded):
        items, _, relaxed = seeded.search(query="completely unrelated zebra")
        assert items == []
        assert relaxed is False
