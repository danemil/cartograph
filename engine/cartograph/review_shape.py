"""Reshape review-context output into the capability contract's typed `data`.

Design and rationale: ``docs/design/review-context-shape.md``.

The upstream tool returns a nested blob with five list-shaped collections. The
contract permits exactly one pageable collection per envelope, so this module
decides which one that is and what happens to the rest:

- ``data.items``    impacted nodes — the review work queue, the thing an agent
                    walks through and may need to continue. This is what pages.
- ``data.summary``  risk, counts, guidance, savings. Bounded, never paged.
- ``data.facets``   changed files, impacted files, changed nodes, edge totals.
                    Bounded context with honest totals, never paged.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

#: Cap for saved_percent. A response is never free, so 100% is never true —
#: and it is the number the project is marketed on.
_MAX_SAVED_PERCENT = 99


def _relativise(path: Optional[str], root: Optional[Path]) -> Optional[str]:
    """Absolute paths are noise in an agent's context; make them repo-relative."""
    if not path or root is None:
        return path
    try:
        return str(Path(path).relative_to(root))
    except (ValueError, TypeError):
        return path


def _node_to_item(node: dict[str, Any], root: Optional[Path]) -> dict[str, Any]:
    """Map a graph node onto the contract's stable item fields."""
    return {
        # qualified_name, not the numeric row id: it is stable across rebuilds
        # and is what an agent can feed back into `carto query`.
        "id": node.get("qualified_name") or node.get("name"),
        "title": node.get("name"),
        "kind": node.get("kind"),
        "location": {
            "file": _relativise(node.get("file_path"), root),
            "line_start": node.get("line_start"),
            "line_end": node.get("line_end"),
        },
        "metadata": {
            "language": node.get("language"),
            "parent": node.get("parent_name"),
            "is_test": node.get("is_test"),
            "node_id": node.get("id"),
        },
    }


def _edge_to_dict(edge: dict[str, Any], root: Optional[Path]) -> dict[str, Any]:
    return {
        "kind": edge.get("kind"),
        "source": edge.get("source"),
        "target": edge.get("target"),
        "file": _relativise(edge.get("file_path"), root),
        "line": edge.get("line"),
        "confidence": edge.get("confidence_tier") or edge.get("confidence"),
    }


def _shape_savings(raw: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Normalise the context-savings block, or return None *explicitly*.

    Upstream omits the key entirely when no token baseline could be computed —
    which makes "not measured" indistinguishable from "no saving", and silently
    drops the project's headline number. The caller always emits the key, so an
    agent can tell the two apart.
    """
    if not raw:
        return None
    saved = int(raw.get("saved_tokens", 0))
    reported = int(raw.get("saved_percent", 0))
    percent = min(reported, _MAX_SAVED_PERCENT)
    out: dict[str, Any] = {
        "estimated": bool(raw.get("estimated", True)),
        "saved_tokens": saved,
        "saved_percent": percent,
    }
    # A ratio is the honest form of the claim ("12.4x"), but it may only be
    # derived from the *reported* percent — never the capped one. Deriving it
    # from a capped 99 yields "100.0x", which is far worse than saying nothing.
    # Above the cap the upstream percent has already rounded away the precision
    # a ratio needs, so no ratio is emitted at all.
    if 0 < reported <= _MAX_SAVED_PERCENT:
        out["ratio"] = f"{round(100 / (100 - reported), 1)}x"
    return out


def shape_review_context(
    raw: dict[str, Any], repo_root: Optional[str] = None
) -> dict[str, Any]:
    """Map `get_review_context(detail_level='standard'|'full')` onto `data`."""
    root = Path(repo_root) if repo_root else None
    context = raw.get("context") or {}
    graph = context.get("graph") or {}

    impacted = graph.get("impacted_nodes") or []
    items = [_node_to_item(n, root) for n in impacted]

    savings = _shape_savings(raw.get("context_savings"))
    summary: dict[str, Any] = {
        "headline": raw.get("summary"),
        "changed_file_count": context.get("changed_files_total"),
        "impacted_file_count": context.get("impacted_files_total"),
        "review_guidance": context.get("review_guidance"),
        # Always present, even as null — see _shape_savings.
        "context_savings": savings,
    }
    if savings is None:
        summary["context_savings_unavailable"] = (
            "No token baseline could be computed for the changed files; "
            "savings were not measured (this is not a claim of zero saving)."
        )

    facets: dict[str, Any] = {
        "changed_files": {
            "items": [_relativise(f, root) for f in (context.get("changed_files") or [])],
            "total": context.get("changed_files_total"),
        },
        "impacted_files": {
            "items": [_relativise(f, root) for f in (context.get("impacted_files") or [])],
            "total": context.get("impacted_files_total"),
        },
        "changed_nodes": {
            "items": [_node_to_item(n, root) for n in (graph.get("changed_nodes") or [])],
            "total": graph.get("changed_nodes_total"),
        },
        "edges": {
            "items": [_edge_to_dict(e, root) for e in (graph.get("edges") or [])],
            "total": graph.get("edges_total"),
            # Graph depth belongs to `carto impact`, which is exactly "the blast
            # radius of a change" and already takes --depth.
            "more": "carto impact --files <file> --depth N",
        },
    }
    if context.get("source_snippets"):
        facets["source_snippets"] = context["source_snippets"]

    return {
        "items": items,
        "items_total": graph.get("impacted_nodes_total"),
        "summary": summary,
        "facets": facets,
    }


def shape_review_summary(
    raw: dict[str, Any], repo_root: Optional[str] = None
) -> dict[str, Any]:
    """Map `get_review_context(detail_level='minimal')` onto `data`.

    A separate command rather than a flag: this is a different typed response,
    not less of the same one. `next_tool_suggestions` is dropped — it named MCP
    tools that no longer exist, and choosing the next step is the skill's job.
    """
    savings = _shape_savings(raw.get("context_savings"))
    out: dict[str, Any] = {
        "headline": raw.get("summary"),
        "risk": raw.get("risk"),
        "changed_file_count": raw.get("changed_file_count"),
        "impacted_file_count": raw.get("impacted_file_count"),
        "test_gaps": raw.get("test_gaps"),
        # key_entities mixes symbol names with file paths; the paths arrive
        # absolute, which is noise in an agent's context.
        "key_entities": [
            _relativise(e, Path(repo_root) if repo_root else None)
            for e in (raw.get("key_entities") or [])
        ],
        "context_savings": savings,
    }
    if savings is None:
        out["context_savings_unavailable"] = (
            "No token baseline could be computed for the changed files; "
            "savings were not measured (this is not a claim of zero saving)."
        )
    return out
