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
