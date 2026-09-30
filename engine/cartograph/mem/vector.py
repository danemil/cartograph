"""The optional sqlite-vec seam.

Every call here answers "no" rather than raising when the extension is absent,
because absent is the *expected* state: the target machines install from a
private repo under default-deny egress, and a loadable SQLite extension is not
something they are guaranteed to have. A memory store that only works with
sqlite-vec present would be a memory store that does not work.

So the whole vector path is behind this module, and there is exactly one rule
for its callers: if anything here returns falsy, the search that follows is
keyword search and must say so. `search_mode` is the field an agent uses to
decide how much to trust a hit, and reporting "semantic" for FTS5 rows is the
single error the contract exists to prevent.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Named once so the store and the status command agree on what they are
#: looking for, and on what to tell the agent when it is missing.
EXTENSION = "sqlite-vec"


def load(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec into this connection. False if it is not available.

    Three distinct absences collapse to one answer — the package is not
    installed, the interpreter was built without loadable-extension support,
    or SQLite refused the load — because the caller's recovery is identical in
    all three: search without vectors and report keyword. Which one it was is
    :func:`unavailable_reason`'s job, for the status line.
    """
    return _try_load(conn) is None


def unavailable_reason() -> str:
    """Why :func:`load` answers no here, in words a person can act on.

    Asked of a scratch connection rather than remembered from the last load,
    so the answer describes this interpreter and not whichever connection
    happened to be opened first. The second case is the one that bit: the
    python.org macOS interpreter has no ``enable_load_extension`` at all, and a
    payload frozen from it reported "not installed" for a package it carried.
    """
    return _try_load(sqlite3.connect(":memory:")) or f"{EXTENSION} loaded"


def _try_load(conn: sqlite3.Connection) -> Optional[str]:
    try:
        import sqlite_vec
    except ImportError:
        return f"{EXTENSION} is not installed"
    if not hasattr(conn, "enable_load_extension"):
        return (
            f"this Python's sqlite3 cannot load extensions, so {EXTENSION} "
            "cannot be used"
        )
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        return None
    except sqlite3.Error as exc:
        logger.debug("%s present but not loadable: %s", EXTENSION, exc)
        return f"{EXTENSION} is installed but SQLite refused to load it: {exc}"
    finally:
        try:
            # Only stops *further* loads; what was loaded above stays loaded.
            conn.enable_load_extension(False)
        except (AttributeError, sqlite3.Error):
            logger.debug("could not re-disable extension loading")


def serialize(vector: list[float]) -> Any:
    """Pack a vector the way sqlite-vec expects it.

    Deliberately the library's own function rather than an equivalent
    ``struct.pack``: the wire format belongs to sqlite-vec, and a local copy of
    it would be a second definition to keep in step with a dependency we do not
    control. Only ever called on a path where ``load`` already succeeded.
    """
    import sqlite_vec

    return sqlite_vec.serialize_float32(vector)


def ensure_table(conn: sqlite3.Connection, dimension: int) -> None:
    """Create the vector index if it is missing.

    The dimension is fixed at creation because vec0 declares it in the table
    definition. A store whose embeddings came from a different model therefore
    cannot be searched by a new one — ``store`` records the dimension and
    refuses the vector path on a mismatch rather than returning neighbours
    computed against two incompatible spaces.
    """
    # Cosine, not vec0's default L2: the scores then read as similarity, which
    # is what the floor in the store is written in.
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS observations_vec USING vec0("  # noqa: S608
        f"embedding float[{int(dimension)}] distance_metric=cosine)"
    )


def drop_table(conn: sqlite3.Connection) -> None:
    """Remove the vector index. The observations it was derived from stay."""
    conn.execute("DROP TABLE IF EXISTS observations_vec")


def missing(conn: sqlite3.Connection, limit: Optional[int]) -> list[tuple[int, str, str]]:
    """Observations without a vector, newest first: ``(rowid, title, body)``.

    Newest first because a capped pass should spend itself on what a person
    is likeliest to search for next — what they did last.
    """
    rows = conn.execute(
        "SELECT rowid, title, body FROM observations "
        "WHERE rowid NOT IN (SELECT rowid FROM observations_vec) "
        "ORDER BY created_at DESC LIMIT ?",
        (-1 if limit is None else int(limit),),
    ).fetchall()
    return [(int(r[0]), r[1] or "", r[2] or "") for r in rows]


def upsert(conn: sqlite3.Connection, rowid: int, vector: list[float]) -> None:
    """Attach a vector to one observation row."""
    conn.execute("DELETE FROM observations_vec WHERE rowid = ?", (rowid,))
    conn.execute(
        "INSERT INTO observations_vec(rowid, embedding) VALUES (?, ?)",
        (rowid, serialize(vector)),
    )


def count(conn: sqlite3.Connection) -> int:
    """How many observations carry a vector. Zero means no semantic path."""
    try:
        return int(conn.execute("SELECT count(*) FROM observations_vec").fetchone()[0])
    except sqlite3.Error:
        # The table is created on first write, so its absence is the ordinary
        # state of a store nothing has embedded yet — not a fault.
        return 0


def knn(
    conn: sqlite3.Connection, vector: list[float], limit: int
) -> Optional[list[tuple[int, float]]]:
    """Nearest neighbours as ``(rowid, score)``, best first, or None on failure.

    ``None`` and ``[]`` mean different things and the caller must distinguish
    them: an empty list is "the index answered, nothing was near", which is a
    semantic answer; ``None`` is "the index did not answer", which means the
    response must degrade to keyword.

    Scores are ``1 - distance`` so that higher is better, matching the
    convention the rest of the engine's search results already use; with the
    cosine metric that is the cosine similarity itself.
    """
    try:
        rows = conn.execute(
            "SELECT rowid, distance FROM observations_vec "
            "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (serialize(vector), int(limit)),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.debug("vector query failed, degrading to keyword: %s", exc)
        return None
    return [(int(row[0]), 1.0 - float(row[1])) for row in rows]
