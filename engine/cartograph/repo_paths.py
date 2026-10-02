"""Make the paths in an agent-facing response repo-relative.

Absolute paths are machine-specific noise. An agent pays for the checkout
prefix on every row of every call, and the prefix tells it nothing it can act
on. These helpers strip it, and ``relativise_result`` applies them across a
whole tool result at the emit boundary.

The shortened form still round-trips: ``carto query`` re-anchors a
repo-relative target against the repo root (``tools/query.py`` tries
``store.get_node(target)``, then ``store.get_node(root / target)``), so an id
an agent reads here resolves when it hands it straight back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

#: Fields whose value is a path, or a qualified name whose first `::` segment
#: is one. Named explicitly rather than sniffed, because the same response may
#: carry file *content* — review-context's source snippets — that legitimately
#: quotes an absolute path, and rewriting a quoted source line would be a lie.
_PATH_FIELDS = frozenset(
    {
        # Node identity, as emitted by graph.node_to_dict.
        "name",
        "qualified_name",
        "parent_name",
        "file_path",
        "file",
        # A File node's signature is its own path; a function's is not, and
        # falls through relativise() untouched.
        "signature",
        # Edge endpoints, and the unresolved candidates an edge carries.
        "source",
        "target",
        "ambiguous_targets",
        "unresolved_targets",
        # Bare path or qualified-name lists.
        "file_paths",  # the files an observation was recorded about
        "changed_files",
        "impacted_files",
        "members",
        "symbols",
        "key_entities",
        "importer",
        "import_target",
        "start_node",
    }
)


def relativise(path: Optional[str], root: Optional[Path]) -> Optional[str]:
    """Absolute paths are noise in an agent's context; make them repo-relative.

    Emitted in POSIX form, not the platform's. The graph stores forward slashes
    as identity (see ``parser.normalize_file_path``), and an ``id`` here is
    meant to be handed straight back to ``carto query`` — so a Windows-shaped
    ``engine\\cartograph\\cli.py`` would look right and resolve to nothing.
    """
    if not path or root is None:
        return path
    try:
        return Path(path).relative_to(root).as_posix()
    except (ValueError, TypeError):
        pass
    # A graph built at another path (a moved checkout, another mount) stores
    # paths under that root; they are still this checkout's files.
    anchor = anchor_of(root)
    if anchor is not None:
        try:
            return Path(path).relative_to(anchor).as_posix()
        except (ValueError, TypeError):
            pass
    return path


def relativise_qualified(name: Optional[str], root: Optional[Path]) -> Optional[str]:
    """Relativise the path half of a qualified name, keeping the rest intact.

    A qualified name is ``<path>`` or ``<path>::<symbol>``; only the path is
    absolute. The suffix is what makes the name resolvable, so it is carried
    through untouched — and `carto query` re-anchors a repo-relative target
    against the repo root, so the shortened form still round-trips.

    The partition is on the FIRST ``::``, so a nested symbol
    (``file.py::Outer::Inner``) stays whole, and separators are never
    normalised inside the symbol half: a PHP namespace identifier
    (``App\\Domain\\Job``) legitimately contains backslashes.
    """
    if not name:
        return name
    path, separator, symbol = name.partition("::")
    return f"{relativise(path, root)}{separator}{symbol}"


def _rewrite(value: Any, root: Path) -> Any:
    """Relativise one path-bearing field: a string, or a list of them."""
    if isinstance(value, str):
        return relativise_qualified(value, root)
    if isinstance(value, list):
        return [_rewrite(item, root) for item in value]
    # A nested object under a path-named key is left to the walk.
    return value


def _walk(node: Any, root: Path) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            # Descend first: _rewrite may hand back a new list, and the
            # objects inside it are the ones the walk has to reach.
            _walk(value, root)
            if key in _PATH_FIELDS:
                node[key] = _rewrite(value, root)
    elif isinstance(node, list):
        for item in node:
            _walk(item, root)


def relativise_result(result: Any, repo_root: "str | Path | None") -> Any:
    """Relativise every path-bearing field of a tool result, in place.

    Idempotent: a value that is already relative is not under ``repo_root``,
    so ``relativise`` leaves it alone. That matters because review-context
    arrives here already shaped, and must not be shortened twice.
    """
    if repo_root is None:
        return result
    _walk(result, Path(repo_root))
    return result


# ---------------------------------------------------------------------------
# A graph whose checkout has moved
# ---------------------------------------------------------------------------
#
# The graph stores absolute paths, anchored at the root it was built at. A
# checkout copied elsewhere with its `.cartograph/`, or mounted at another path
# (a Dev Container's /workspaces/<name>), keeps those paths. Every comparison
# with the working tree then fails: coverage counted none of the graph's files
# and told the user a complete answer was partial. Reads therefore compare
# repo-relative paths, against whichever root the stored paths are under;
# nothing is rewritten on a read, because two mounts of one checkout would
# otherwise rewrite the graph back and forth on every query.

#: Current root (POSIX) -> the root the graph's stored paths are under, for
#: graphs that were built somewhere else. Only moved graphs have an entry.
_ANCHORS: dict[str, Path] = {}
_SEEN: set[tuple[str, str]] = set()

#: Stored files sampled to work out where a moved graph was built. Votes, so
#: one file that happens to exist at a shorter suffix cannot decide it.
_ANCHOR_SAMPLE = 200


def _posix(path: "str | Path") -> str:
    return str(path).replace("\\", "/").rstrip("/")


def infer_anchor(stored_files: "list[str]", root: Path) -> Optional[Path]:
    """The root ``stored_files`` are under, when it is not ``root``.

    None when any stored file is already under ``root`` (the graph was built
    here), or when no stored file maps onto the working tree. Otherwise the
    prefix that, removed from a stored path, leaves a path that exists under
    ``root`` — the longest such remainder per file, the commonest prefix over
    a sample, and only when at least half the sample agrees. Decided from the
    graph and ``stat`` calls only.
    """
    from collections import Counter
    from pathlib import PurePosixPath

    if not stored_files:
        return None
    heads = {_posix(root) + "/", _posix(Path(root).resolve()) + "/"}
    if any(_posix(f).startswith(h) for f in stored_files for h in heads):
        return None
    votes: Counter[str] = Counter()
    for stored in stored_files[:_ANCHOR_SAMPLE]:
        parts = PurePosixPath(_posix(stored)).parts
        for i in range(1, len(parts)):
            if (Path(root) / "/".join(parts[i:])).is_file():
                votes[PurePosixPath(*parts[:i]).as_posix()] += 1
                break
    if not votes:
        return None
    anchor, count = votes.most_common(1)[0]
    # Most of the graph has to be this tree. A graph of some other repository
    # that shares a few file names is not a moved copy of this one, and
    # rebasing it on update would graft it onto the wrong tree.
    if count * 2 < min(len(stored_files), _ANCHOR_SAMPLE):
        return None
    return Path(anchor)


def register_anchor(store: Any, root: "str | Path | None") -> Optional[Path]:
    """Work out, once per graph and root, where the graph's paths are anchored.

    Returns the other root for a moved graph, else None. Registered so that
    ``relativise`` and ``graph_path`` apply it without every caller having to
    carry it.
    """
    if root is None or store is None:
        return None
    root = Path(root)
    key = (str(getattr(store, "db_path", id(store))), _posix(root))
    if key not in _SEEN:
        _SEEN.add(key)
        anchor = infer_anchor(store.get_file_marker_paths(), root)
        if anchor is not None:
            _ANCHORS[_posix(root)] = anchor
            _ANCHORS[_posix(root.resolve())] = anchor
        else:
            _ANCHORS.pop(_posix(root), None)
            _ANCHORS.pop(_posix(root.resolve()), None)
    return _ANCHORS.get(_posix(root))


def anchor_of(root: "str | Path | None") -> Optional[Path]:
    """The registered anchor for ``root``, if its graph was built elsewhere."""
    if root is None:
        return None
    return _ANCHORS.get(_posix(root))


def forget_anchors() -> None:
    """Drop what this process learnt; after a rebase the stored paths moved."""
    _ANCHORS.clear()
    _SEEN.clear()


def graph_path(path: "str | Path", root: "str | Path") -> str:
    """The stored spelling of a working-tree path: repo-relative or absolute
    under ``root`` in, the graph's own absolute POSIX path out."""
    root = Path(root)
    base = anchor_of(root) or root
    p = Path(path)
    if p.is_absolute():
        for r in (root, root.resolve()):
            try:
                return (base / p.relative_to(r)).as_posix()
            except ValueError:
                continue
        return str(path).replace("\\", "/")
    return (base / p).as_posix()


def working_path(stored: str, root: "str | Path") -> Path:
    """Where a stored path is in this checkout, for reading the file."""
    anchor = anchor_of(root)
    p = Path(stored)
    if anchor is not None and p.is_absolute():
        try:
            return Path(root) / p.relative_to(anchor)
        except ValueError:
            pass
    return p
