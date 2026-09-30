"""Storage for observations: schema, writes, and the search behind `mem search`.

One SQLite file next to the graph (`.cartograph/memory.db`), for the reason the
graph's own database is one file: the target machines install from a private
repo under default-deny egress, so anything that needs a daemon, a service, or
a download at query time is not a candidate.

The file is separate from `graph.db` on purpose. The two have unrelated
lifecycles — `carto build` rewrites the graph wholesale, while observations are
append-only and must survive every rebuild — and a shared file would make a
rebuild a data-loss risk for something the graph does not own.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from . import vector as _vector

logger = logging.getLogger(__name__)

#: ``mem_meta`` key: why the last attempt to embed failed, until one succeeds.
_EMBED_FAILURE = "embedding_failure"

#: Beside `graph.db` in whatever directory `get_data_dir` resolves for the
#: repository, so `--data-dir` and `CRG_DATA_DIR` move the memory with the
#: graph rather than stranding half of a project's state.
DB_FILENAME = "memory.db"

#: What `--doc-type` filters on. A session or a prompt is a different *kind of
#: record* from an observation, not a different observation type, which is why
#: it is its own column rather than a value of `kind`.
DOC_TYPES = ("observations", "sessions", "prompts")

#: Where an observation's text came from, per T07. `verbatim` is this build's
#: addition: `mem add` records what the caller wrote, and calling that
#: "structural" would claim a summarisation that never happened.
SUMMARY_SOURCES = ("host-agent", "structural", "verbatim")

#: How `mem search` may order. `relevance` is the default because a search that
#: ignores its own ranking is a list, not a search; the two date orders are
#: what T10 specifies for walking a session in the order it happened.
ORDER_BY = ("relevance", "date_desc", "date_asc")

#: UTC, microseconds, lexicographically sortable — so ORDER BY on the text
#: column is chronological and needs no parsing.
_TIMESTAMP = "%Y-%m-%dT%H:%M:%S.%fZ"

#: Distinguishes "not looked up yet" from "looked up, and there is none".
_UNRESOLVED = object()

#: Long enough to judge a hit by, short enough that twenty of them still fit a
#: budget. `fit` will shed the tail of the list if they do not.
_MAX_SNIPPET_CHARS = 240

#: Rows a search or a single write may embed on its way past. Vectors are
#: filled in lazily so an existing store needs no rebuild step anyone has to
#: know about, but a read must not turn into a minutes-long backfill: at the
#: measured ~3 ms a row this bounds the detour to about a second. `mem sync`
#: embeds without a bound.
BACKFILL_CAP = 256

#: Nearest neighbours below this cosine similarity are dropped. A vector index
#: always has a nearest neighbour, however unrelated; without a floor every
#: search returns `limit` rows and an agent cannot tell recall from filler.
#: Set from all-MiniLM-L6-v2 on memory-shaped text (a title and a few
#: sentences): unrelated queries measured at most 0.17, reworded ones that
#: share no keyword 0.32-0.42 (docs/memory-design.md).
MIN_SIMILARITY = 0.25


def db_path(repo_root: "str | Path", *, create: bool = False) -> Path:
    """Where this repository's observations live.

    ``create=False`` for every read path: resolving the location must not bring
    the store into existence, or "is there a memory store?" could never be
    answered no.
    """
    from ..incremental import get_data_dir

    return get_data_dir(Path(repo_root), create=create) / DB_FILENAME


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """v1: observations, their FTS5 index, and the triggers that keep it true."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS observations (
            id TEXT PRIMARY KEY,
            project TEXT NOT NULL,
            session TEXT,
            doc_type TEXT NOT NULL DEFAULT 'observations',
            kind TEXT NOT NULL DEFAULT 'note',
            title TEXT NOT NULL,
            body TEXT NOT NULL DEFAULT '',
            file_paths TEXT NOT NULL DEFAULT '[]',
            platform_source TEXT,
            summary_source TEXT NOT NULL DEFAULT 'verbatim',
            created_at TEXT NOT NULL
        )
    """)
    # Every one of these backs a documented `mem search` filter. The compound
    # index leads with project because a shared data directory holds several
    # projects' observations in one file, so it is the filter that eliminates
    # the most rows.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_observations_created "
        "ON observations(created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_observations_project "
        "ON observations(project, created_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_observations_session "
        "ON observations(session, created_at DESC)"
    )
    # External-content FTS5: the text is stored once, in `observations`. The
    # triggers are what make that safe — a contentless index that drifts from
    # its table returns rows that no longer exist, and the join silently drops
    # them, so a search would under-report without anything saying so.
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
            title, body,
            content='observations', content_rowid='rowid',
            tokenize='porter unicode61'
        )
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS observations_ai AFTER INSERT ON observations
        BEGIN
            INSERT INTO observations_fts(rowid, title, body)
            VALUES (new.rowid, new.title, new.body);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS observations_ad AFTER DELETE ON observations
        BEGIN
            INSERT INTO observations_fts(observations_fts, rowid, title, body)
            VALUES ('delete', old.rowid, old.title, old.body);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS observations_au AFTER UPDATE ON observations
        BEGIN
            INSERT INTO observations_fts(observations_fts, rowid, title, body)
            VALUES ('delete', old.rowid, old.title, old.body);
            INSERT INTO observations_fts(rowid, title, body)
            VALUES (new.rowid, new.title, new.body);
        END
    """)
    # Vector bookkeeping, not vectors: which model wrote the index, and at what
    # width. vec0 fixes the dimension at creation, so a store embedded by one
    # model cannot be searched by another, and this is what lets that be
    # detected rather than discovered as wrong neighbours.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mem_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    logger.info("Memory migration v1: created observations, FTS index and triggers")


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migrate_v1,
}

LATEST_VERSION = max(MIGRATIONS.keys())


def run_migrations(conn: sqlite3.Connection) -> None:
    """Run pending migrations in order, each in its own transaction.

    Same shape as ``cartograph.migrations``, with one difference: the version
    lives in ``PRAGMA user_version`` rather than a metadata table. The graph's
    metadata table exists to hold build facts and the version rides along in
    it; this database has no such table, and creating one whose only row is the
    schema version would need its own bootstrap — a table that must exist
    before the migration that would have created it.
    """
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if current >= LATEST_VERSION:
        return
    for version in sorted(MIGRATIONS):
        if version <= current:
            continue
        try:
            MIGRATIONS[version](conn)
            # PRAGMA takes no parameters, so the value is interpolated. It is
            # an int from this module's own registry, never from input.
            conn.execute(f"PRAGMA user_version = {int(version)}")  # noqa: S608
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            logger.error("Memory migration v%d failed, rolling back", version)
            raise


# ---------------------------------------------------------------------------
# Query text
# ---------------------------------------------------------------------------

_WORD = re.compile(r"\w+", re.UNICODE)


#: Dropped from the relaxed (OR) retry only — see :func:`fts_match_any`. Under
#: AND they are harmless; under OR each one matches most of the corpus. Kept
#: deliberately short: this is about words that carry no retrieval signal in any
#: query, not a general English stoplist, and over-trimming would lose real
#: terms like "not" from "why not X".
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "did", "do", "does", "for",
    "from", "had", "has", "have", "how", "i", "in", "is", "it", "of", "on", "or",
    "that", "the", "then", "there", "they", "this", "to", "was", "we", "were",
    "what", "when", "where", "which", "who", "why", "will", "with", "you",
})


def fts_match(query: str) -> str:
    """Turn an agent's question into an FTS5 MATCH expression, or "".

    Reduced to quoted word tokens joined by AND. The reduction is the point:
    the query is prose an agent wrote, and FTS5 would otherwise read `-`, `*`,
    `:`, `NEAR` and an unbalanced quote as syntax. That raises an
    OperationalError, which would reach the agent as an internal error for a
    question that was perfectly well formed.

    The cost is that phrase and prefix syntax cannot be passed through. That is
    the right trade for a surface an agent drives: it can narrow with the
    filters instead, and none of them can make the query unparseable.
    """
    tokens = _WORD.findall(query or "")
    return " AND ".join(f'"{token}"' for token in tokens)


def fts_match_any(query: str) -> str:
    """The same expression with OR, for the second attempt.

    FTS5 requires every term, which is right when a caller names the words they
    expect. It is wrong for the way this surface is actually driven: an agent
    recalls by asking a question, and a question carries words the record never
    had — "why did we choose offset paging" misses a note titled "Chose offset
    paging" on the strength of "why" and "did". Requiring all terms turns one
    absent filler word into no answer at all.

    So a query that matches nothing is retried with OR rather than abandoned.
    Ranking already puts the rows sharing the most terms first, which is what
    made AND look necessary.

    Stopwords are dropped from THIS expression only. Under OR they are what a
    spurious hit is made of: "what database did we reject and why" matched a
    note about FalkorDB on the strength of "and". A row returned because it
    contains "and" is luck, and an agent cannot tell luck from retrieval — an
    honest miss is worth more, and is what tells it to rephrase.
    """
    tokens = [t for t in _WORD.findall(query or "") if t.lower() not in _STOPWORDS]
    return " OR ".join(f'"{token}"' for token in tokens)


def _end_of_day(value: str) -> str:
    """Make a bare ``--date-end YYYY-MM-DD`` include the day it names.

    Compared as text against a full timestamp, `2026-09-17` sorts before
    everything recorded *on* the 17th, so the obvious call silently excludes
    the day the caller asked for.
    """
    return f"{value}T23:59:59.999999Z" if len(value) == 10 else value


#: Rows embedded and committed together, so an interrupted backfill keeps
#: what it finished and a failure loses at most one chunk.
_EMBED_CHUNK = 64



# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class MemoryStore:
    """Observations for one repository."""

    def __init__(self, path: "str | Path") -> None:
        self.path = Path(path)
        self._conn = sqlite3.connect(str(self.path), timeout=30)
        self._conn.row_factory = sqlite3.Row
        run_migrations(self._conn)
        self._vec_loaded: Optional[bool] = None
        self._provider: Any = _UNRESOLVED

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    # -- vectors ----------------------------------------------------------

    def _vec_loaded_ok(self) -> bool:
        if self._vec_loaded is None:
            self._vec_loaded = _vector.load(self._conn)
        return self._vec_loaded

    def _embedding_provider(self) -> Any:
        """The configured embedding provider, or None.

        Resolved only after sqlite-vec has loaded, so a machine without the
        extension never pays for importing the embedding stack — and never
        risks a provider constructor reaching for a model it would have to
        fetch.
        """
        if self._provider is _UNRESOLVED:
            from ..embeddings import get_provider

            try:
                self._provider = get_provider()
            except ValueError as exc:
                # Credentials named but incomplete. Not this command's problem
                # to fix, and not a reason to fail a keyword search.
                logger.debug("no embedding provider: %s", exc)
                self._provider = None
        return self._provider

    def _embedding_failed(self, context: str, exc: BaseException) -> None:
        """Log one line, keep the detail for debug, and remember the reason.

        Remembered in ``mem_meta`` because ``mem status`` never loads the model,
        and without it would report "not embedded yet" for a runtime that can
        never embed on this machine.
        """
        from ..embeddings import embedding_failure_reason

        reason = embedding_failure_reason(exc)
        logger.debug("%s", context, exc_info=exc)
        logger.warning("%s: %s", context, reason)
        if self.get_meta(_EMBED_FAILURE) != reason:
            self.set_meta(_EMBED_FAILURE, reason)

    def _embedding_worked(self) -> None:
        if self.get_meta(_EMBED_FAILURE) is not None:
            self._conn.execute("DELETE FROM mem_meta WHERE key = ?", (_EMBED_FAILURE,))
            self._conn.commit()

    def semantic_status(self) -> tuple[bool, Optional[str]]:
        return self._semantic_status(report_failure=True)

    def _semantic_status(self, *, report_failure: bool) -> tuple[bool, Optional[str]]:
        """Whether embeddings can participate, and if not, what is missing.

        The reason is reported by ``mem status`` because "why is my search
        keyword-only" is otherwise unanswerable from the outside: three
        independent things have to be true, and the envelope's `search_mode`
        can only say that the answer is no.

        Never loads the model: the provider's identity comes from its
        manifest, so asking costs a JSON read. A runtime that failed to load
        is known from the last attempt, recorded by ``_embedding_failed``.
        The search path asks without that record (``report_failure=False``)
        and tries anyway, so a runtime that works again is noticed by using it.
        """
        if not self._vec_loaded_ok():
            return False, _vector.unavailable_reason()
        if self._embedding_provider() is None:
            from ..embeddings import local_unavailable_reason

            return False, local_unavailable_reason()
        failure = self.get_meta(_EMBED_FAILURE) if report_failure else None
        if failure:
            return False, failure
        if _vector.count(self._conn) == 0:
            return False, "no observation has been embedded yet"
        stored = self._vector_meta()
        current = self._provider_identity()
        if stored and current and stored != current:
            return False, (
                f"the index was written by {stored[0]} at {stored[1]} dimensions, "
                f"but {current[0]} is configured now"
            )
        return True, None

    def vector_count(self) -> int:
        """Observations that carry a vector, for `mem status`."""
        return _vector.count(self._conn) if self._vec_loaded_ok() else 0

    def _provider_identity(self) -> Optional[tuple[str, int]]:
        provider = self._embedding_provider()
        if provider is None:
            return None
        try:
            return provider.name, int(provider.dimension)
        except Exception as exc:  # noqa: BLE001 — provider internals vary
            # `dimension` loads the model on the local provider, so this is
            # where "installed but unusable" surfaces. Reported, not swallowed:
            # the caller degrades to keyword and says so.
            self._embedding_failed("embedding provider unusable", exc)
            return None

    def _vector_meta(self) -> Optional[tuple[str, int]]:
        rows = dict(
            self._conn.execute(
                "SELECT key, value FROM mem_meta WHERE key IN "
                "('embedding_provider', 'embedding_dimension')"
            ).fetchall()
        )
        name, dimension = rows.get("embedding_provider"), rows.get("embedding_dimension")
        if not name or not dimension:
            return None
        return name, int(dimension)

    def embed_missing(self, *, limit: Optional[int] = None) -> int:
        """Give observations that lack a vector one. How many were written.

        The one place vectors are written, so every path — a write, a sync, a
        search meeting rows nobody embedded — fills the index the same way. A
        store from before embeddings shipped is therefore backfilled by using
        it, with no rebuild step to know about.

        An index written by a different model is rebuilt rather than refused:
        vectors are derived from the observations, which are all still here,
        and two models' vectors in one index would give neighbours computed
        across incompatible spaces. Until the rebuild finishes the index holds
        fewer rows, never mixed ones.
        """
        if not self._vec_loaded_ok():
            return 0
        identity = self._provider_identity()
        if identity is None:
            return 0
        name, dimension = identity
        stored = self._vector_meta()
        if stored and stored != identity:
            logger.info("re-embedding: index belongs to %s, provider is %s", stored[0], name)
            _vector.drop_table(self._conn)
            self._conn.execute(
                "DELETE FROM mem_meta WHERE key IN ('embedding_provider', 'embedding_dimension')"
            )
            self._conn.commit()
        try:
            _vector.ensure_table(self._conn, dimension)
            pending = _vector.missing(self._conn, limit)
        except sqlite3.Error as exc:
            # A broken index is a reason to search by keyword, which the
            # caller will then report — not a reason to fail the command.
            logger.warning("vector index unusable, not embedding: %s", exc)
            return 0
        # Read, then release: the model runs with no transaction open, so a
        # hook capturing a prompt meanwhile is never kept waiting on it.
        self._conn.commit()
        if not pending:
            return 0
        provider = self._embedding_provider()
        written = 0
        for start in range(0, len(pending), _EMBED_CHUNK):
            chunk = pending[start:start + _EMBED_CHUNK]
            try:
                vectors = provider.embed([f"{title}\n{body}" for _, title, body in chunk])
                for (rowid, _, _), vector_value in zip(chunk, vectors):
                    _vector.upsert(self._conn, rowid, vector_value)
            except Exception as exc:  # noqa: BLE001 — provider internals vary
                # An observation without a vector is still an observation, and
                # the next pass will try it again.
                self._conn.rollback()
                self._embedding_failed("observations left without a vector", exc)
                break
            self._conn.executemany(
                "INSERT OR REPLACE INTO mem_meta(key, value) VALUES (?, ?)",
                [("embedding_provider", name), ("embedding_dimension", str(dimension))],
            )
            self._conn.commit()
            written += len(chunk)
        if written:
            self._embedding_worked()
        return written

    def _has_vector(self, rowid: int) -> bool:
        try:
            return self._conn.execute(
                "SELECT 1 FROM observations_vec WHERE rowid = ?", (rowid,)
            ).fetchone() is not None
        except sqlite3.Error:
            return False

    # -- writes -----------------------------------------------------------

    def _next_timestamp(self) -> str:
        """A creation time that is always later than every stored one.

        Cursors bind to the provenance block, and this store's provenance is
        its latest timestamp — that is what makes a cursor issued before a
        write refuse to continue after it. Two rows sharing a timestamp would
        put a hole in that guarantee, so the clock is nudged rather than
        trusted to have moved.
        """
        now = datetime.now(timezone.utc).strftime(_TIMESTAMP)
        latest = self._conn.execute(
            "SELECT max(created_at) FROM observations"
        ).fetchone()[0]
        if latest and latest >= now:
            moved = datetime.strptime(latest, _TIMESTAMP) + timedelta(microseconds=1)
            now = moved.strftime(_TIMESTAMP)
        return now

    def add(
        self,
        *,
        project: str,
        title: str,
        body: str = "",
        kind: str = "note",
        session: Optional[str] = None,
        doc_type: str = "observations",
        platform_source: Optional[str] = None,
        summary_source: str = "verbatim",
        file_paths: Sequence[str] = (),
        embed: bool = True,
    ) -> dict[str, Any]:
        """Record one observation and return it.

        ``embed=False`` is for the prompt-capture hook, which runs inside the
        host's turn: loading a model there would turn ~1 ms of work into most
        of a second on every prompt. Its rows are embedded by the next sync,
        write or search instead (:meth:`embed_missing`).
        """
        created_at = self._next_timestamp()
        paths = list(file_paths)
        # The id is content-addressed including the timestamp, which is unique
        # by construction above — so ids are stable to quote back and two
        # identical notes taken at different moments stay two observations.
        identity = "\x00".join(
            [project, session or "", doc_type, kind, title, body, created_at]
        )
        obs_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        cursor = self._conn.execute(
            "INSERT INTO observations(id, project, session, doc_type, kind, title, "
            "body, file_paths, platform_source, summary_source, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                obs_id, project, session, doc_type, kind, title, body,
                json.dumps(paths), platform_source, summary_source, created_at,
            ),
        )
        self._conn.commit()
        embedded = False
        if embed:
            rowid = int(cursor.lastrowid or 0)
            self.embed_missing(limit=BACKFILL_CAP)
            embedded = self._has_vector(rowid)
        return {
            "id": obs_id,
            "project": project,
            "session": session,
            "doc_type": doc_type,
            "kind": kind,
            "title": title,
            "file_paths": paths,
            "platform_source": platform_source,
            "summary_source": summary_source,
            "created_at": created_at,
            "embedded": embedded,
        }

    # -- reads ------------------------------------------------------------

    def provenance(self) -> dict[str, Any]:
        """What state produced an answer, in the contract's own field names.

        ``built_at`` is the newest observation, which advances on every write
        (see ``_next_timestamp``), so a cursor minted before a write is refused
        after it — the same guarantee a rebuilt graph gives the graph commands.
        ``graph_sha`` is null and stays null: no graph took part in this
        answer, and filling the field with a memory digest would make the two
        commands' provenance blocks mean different things.
        """
        row = self._conn.execute(
            "SELECT max(created_at), count(*) FROM observations"
        ).fetchone()
        return {
            "graph_sha": None,
            "built_at": row[0],
            "schema_version": LATEST_VERSION,
            "observations": int(row[1]),
        }

    def session_count(self, session: str, *, doc_type: Optional[str] = None) -> int:
        """How many observations one session has recorded.

        Here rather than in the caller so the ingestion budget is counted
        against the same rows `mem search --session` returns, through the
        index that already exists for that filter.

        ``doc_type`` narrows it, which is what answers "has this session
        already been summarised?" — one counter serving both budgets rather
        than a second query that could come to disagree about which rows count.
        """
        sql = "SELECT count(*) FROM observations WHERE session = ?"
        params: list[Any] = [session]
        if doc_type:
            sql += " AND doc_type = ?"
            params.append(doc_type)
        return int(self._conn.execute(sql, params).fetchone()[0])

    def session_documents(
        self, session: str, *, doc_type: str, limit: Optional[int] = None
    ) -> list[dict[str, Any]]:
        """One session's rows of one type, oldest first, bodies included.

        Not `search`: that surface takes a query, ranks, and returns snippets,
        and a summariser needs the whole session in the order it happened with
        nothing elided. ``limit`` keeps the MOST RECENT rows — a session that
        ran past the cap has drifted, and its later half is what a future
        search is asking about — while still returning them chronologically,
        because the arc is the thing being summarised.
        """
        sql = (
            "SELECT id, title, body, created_at, platform_source "
            "FROM observations WHERE session = ? AND doc_type = ? "
            "ORDER BY created_at DESC, id ASC"
        )
        params: list[Any] = [session, doc_type]
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in reversed(rows)]

    def get_meta(self, key: str) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM mem_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO mem_meta(key, value) VALUES (?, ?)", (key, value)
        )
        self._conn.commit()

    def add_to_counter(self, key: str, amount: int) -> None:
        """Add *amount* to an integer kept in ``mem_meta``."""
        self.set_meta(key, str(int(self.get_meta(key) or 0) + int(amount)))

    def meta_with_prefix(self, prefix: str) -> dict[str, str]:
        rows = self._conn.execute(
            "SELECT key, value FROM mem_meta WHERE key >= ? AND key < ?",
            (prefix, prefix + "\uffff"),
        ).fetchall()
        return {key: value for key, value in rows}

    def get_by_ids(self, ids: Sequence[str]) -> list[dict[str, Any]]:
        """Whole observations by id, in the order asked, bodies untruncated.

        The second step after search: search returns a snippet per row so a
        list stays cheap, and this returns the full text of only the rows the
        caller chose — the same index-then-detail split claude-mem uses.
        """
        found: dict[str, dict[str, Any]] = {}
        for obs_id in dict.fromkeys(ids):
            row = self._conn.execute(
                "SELECT id, project, session, doc_type, kind, title, body, file_paths, "
                "platform_source, summary_source, created_at FROM observations WHERE id = ?",
                (obs_id,),
            ).fetchone()
            if row is not None:
                item = dict(row)
                item["file_paths"] = json.loads(item.get("file_paths") or "[]")
                found[obs_id] = item
        return [found[obs_id] for obs_id in dict.fromkeys(ids) if obs_id in found]

    def has_document(self, session: str, body: str, *, doc_type: str) -> bool:
        """Whether this session already recorded exactly this body.

        What keeps one prompt one row when the same event reaches capture
        twice — two hook files, or VS Code told to read a Claude-format file as
        well as its own. Exact match on purpose: a person repeating a question
        in a new session asked it twice, and that is worth keeping.
        """
        row = self._conn.execute(
            "SELECT 1 FROM observations WHERE session = ? AND doc_type = ? "
            "AND body = ? LIMIT 1",
            (session, doc_type, body),
        ).fetchone()
        return row is not None

    def unsummarised_sessions(
        self,
        *,
        source_type: str,
        summary_type: str,
        min_rows: int,
        exclude: Iterable[str] = (),
        limit: int,
    ) -> list[str]:
        """Sessions with at least *min_rows* of *source_type* and no *summary_type* row.

        Most recent first: a person returning to a repository is likeliest to
        search for what they did last, and the per-run bound should spend
        itself there rather than on the oldest backlog.
        """
        excluded = list(exclude)
        sql = (
            "SELECT session FROM observations WHERE session IS NOT NULL "
            "AND doc_type = ? "
            + (f"AND session NOT IN ({', '.join('?' * len(excluded))}) " if excluded else "")
            + "AND session NOT IN (SELECT session FROM observations "
            "WHERE doc_type = ? AND session IS NOT NULL) "
            "GROUP BY session HAVING count(*) >= ? "
            "ORDER BY max(created_at) DESC LIMIT ?"
        )
        params: list[Any] = [source_type, *excluded]
        params += [summary_type, int(min_rows), int(limit)]
        return [row[0] for row in self._conn.execute(sql, params).fetchall()]

    def latest_session(self, *, doc_type: str) -> Optional[str]:
        """The session that most recently recorded a row of this type.

        What `mem summarise` with no `--session` means: the caller is a person
        or a host that has just finished working, and naming the session they
        were in is something neither can reliably do.
        """
        row = self._conn.execute(
            "SELECT session FROM observations WHERE session IS NOT NULL "
            "AND doc_type = ? ORDER BY created_at DESC LIMIT 1",
            (doc_type,),
        ).fetchone()
        return row[0] if row else None

    def stats(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT count(*), count(DISTINCT project), count(DISTINCT session), "
            "min(created_at), max(created_at) FROM observations"
        ).fetchone()
        kinds = [
            r[0] for r in self._conn.execute(
                "SELECT kind FROM observations GROUP BY kind ORDER BY count(*) DESC"
            ).fetchall()
        ]
        return {
            "observations": int(row[0]),
            "projects": int(row[1]),
            "sessions": int(row[2]),
            "kinds": kinds,
            "oldest_at": row[3],
            "latest_at": row[4],
        }

    def _filters(
        self,
        *,
        project: Optional[str],
        session: Optional[str],
        doc_type: Optional[str],
        kinds: Sequence[str],
        platform_source: Optional[str],
        date_start: Optional[str],
        date_end: Optional[str],
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("project", project),
            ("session", session),
            ("doc_type", doc_type),
            ("platform_source", platform_source),
        ):
            if value:
                clauses.append(f"o.{column} = ?")
                params.append(value)
        if kinds:
            clauses.append(f"o.kind IN ({','.join('?' for _ in kinds)})")
            params.extend(kinds)
        if date_start:
            clauses.append("o.created_at >= ?")
            params.append(date_start)
        if date_end:
            clauses.append("o.created_at <= ?")
            params.append(_end_of_day(date_end))
        return (" AND ".join(clauses), params)

    def _fts_candidates(
        self, match: str, where: str, params: list[Any], limit: int
    ) -> list[tuple[int, float, str]]:
        """FTS5 hits as ``(rowid, score, snippet)``, best first.

        bm25 is negative and lower is better; it is negated so that every score
        on this surface means the same thing — higher is a better hit —
        whichever retriever produced it.
        """
        sql = (
            "SELECT o.rowid, bm25(observations_fts) AS rank, "
            "snippet(observations_fts, 1, '', '', '…', 16) AS excerpt "
            "FROM observations_fts f JOIN observations o ON o.rowid = f.rowid "
            "WHERE observations_fts MATCH ?"
        )
        if where:
            sql += f" AND {where}"
        sql += " ORDER BY rank ASC, o.created_at DESC, o.id ASC LIMIT ?"
        rows = self._conn.execute(sql, [match, *params, limit]).fetchall()
        return [(int(r[0]), round(-float(r[1]), 9), r[2] or "") for r in rows]

    def _vector_candidates(
        self, query: str, where: str, params: list[Any], limit: int
    ) -> Optional[list[tuple[int, float]]]:
        """Nearest neighbours that also satisfy the filters, or None.

        vec0 cannot apply the filters itself unless they are declared as
        partition columns, so the filter is applied afterwards against the set
        of rows that pass it. A wider k covers the rows that get discarded;
        beyond that a filtered semantic search can return fewer than `limit`
        hits, which is honest — it is what the index had.
        """
        if not self._vec_loaded_ok():
            return None
        self.embed_missing(limit=BACKFILL_CAP)
        available, _ = self._semantic_status(report_failure=False)
        if not available:
            return None
        try:
            vector_value = self._embedding_provider().embed_query(query)
        except Exception as exc:  # noqa: BLE001 — provider internals vary
            self._embedding_failed("query not embedded, degrading to keyword", exc)
            return None
        self._embedding_worked()
        neighbours = _vector.knn(self._conn, vector_value, limit * 4)
        if neighbours is None:
            return None
        neighbours = [pair for pair in neighbours if pair[1] >= MIN_SIMILARITY]
        if not where:
            return neighbours[:limit]
        allowed = {
            int(r[0]) for r in self._conn.execute(
                f"SELECT o.rowid FROM observations o WHERE {where}", params  # noqa: S608
            ).fetchall()
        }
        return [pair for pair in neighbours if pair[0] in allowed][:limit]

    def search(
        self,
        *,
        query: str,
        project: Optional[str] = None,
        session: Optional[str] = None,
        doc_type: Optional[str] = None,
        kinds: Sequence[str] = (),
        platform_source: Optional[str] = None,
        date_start: Optional[str] = None,
        date_end: Optional[str] = None,
        order_by: str = "relevance",
        limit: int = 25,
    ) -> tuple[list[dict[str, Any]], str, bool]:
        """Matching observations, best first, the mode, and whether it relaxed.

        The mode names **which retrievers ran**, not which ones happened to
        return rows. A semantic search that finds nothing still consulted the
        embeddings, and reporting that as keyword would understate the answer
        exactly as reporting keyword results as semantic overstates it.
        """
        where, params = self._filters(
            project=project, session=session, doc_type=doc_type, kinds=kinds,
            platform_source=platform_source, date_start=date_start, date_end=date_end,
        )
        match = fts_match(query)
        fts = self._fts_candidates(match, where, params, limit) if match else []
        relaxed = False
        if match and not fts:
            # Every term was required and one was missing. Widen rather than
            # report nothing: see fts_match_any. Recorded on the response,
            # because an agent that cannot tell a relaxed match from an exact
            # one cannot judge how much to trust the rows.
            any_match = fts_match_any(query)
            if any_match != match:
                fts = self._fts_candidates(any_match, where, params, limit)
                relaxed = bool(fts)
        vectors = self._vector_candidates(query, where, params, limit)

        snippets = {rowid: excerpt for rowid, _, excerpt in fts}
        if vectors is None:
            mode = "fts"
            ordered = [(rowid, score) for rowid, score, _ in fts]
        elif not match:
            mode = "semantic"
            ordered = vectors
        else:
            mode = "hybrid"
            # Reciprocal Rank Fusion, from the engine's own search module: two
            # rankings on incomparable scales (bm25 and cosine distance) can
            # only be merged by rank. Imported here rather than at module level
            # because it arrives with the parser behind it, and the keyword
            # path must not pay for that.
            from ..search import rrf_merge

            ordered = rrf_merge([(r, s) for r, s, _ in fts], vectors)

        rows = self._rows_by_rowid([rowid for rowid, _ in ordered])
        scores = dict(ordered)
        items = [
            self._to_item(rows[rowid], scores.get(rowid), snippets.get(rowid))
            for rowid, _ in ordered
            if rowid in rows
        ]
        if order_by == "date_desc":
            items.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
        elif order_by == "date_asc":
            items.sort(key=lambda item: (item["created_at"], item["id"]))
        return items[:limit], mode, relaxed

    def _rows_by_rowid(self, rowids: Iterable[int]) -> dict[int, sqlite3.Row]:
        ids = list(rowids)
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"SELECT rowid, * FROM observations WHERE rowid IN ({placeholders})",  # noqa: S608
            ids,
        ).fetchall()
        return {int(row["rowid"]): row for row in rows}

    def _to_item(
        self, row: sqlite3.Row, score: Optional[float], snippet: Optional[str]
    ) -> dict[str, Any]:
        """One search hit.

        Flat, not `{id, title, metadata: {...}}`: the envelope schema says data
        is "typed per tool — never an opaque blob", and a metadata bag is how a
        typed response becomes one.
        """
        body = row["body"] or ""
        return {
            "id": row["id"],
            "title": row["title"],
            "kind": row["kind"],
            "doc_type": row["doc_type"],
            "score": score,
            "snippet": snippet or body[:_MAX_SNIPPET_CHARS],
            "project": row["project"],
            "session": row["session"],
            "platform_source": row["platform_source"],
            "summary_source": row["summary_source"],
            "file_paths": json.loads(row["file_paths"] or "[]"),
            "created_at": row["created_at"],
        }

