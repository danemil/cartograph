"""Tools 4, 16: review context, detect changes."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..changes import analyze_changes, parse_diff_ranges, parse_git_diff_ranges  # noqa: F401
from ..context_savings import attach_context_savings, estimate_file_tokens
from ..graph import edge_to_dict, node_to_dict
from ..hints import generate_hints, get_session
from ..incremental import get_changed_files, get_staged_and_unstaged
from ..parser import normalize_file_path
from ._common import (
    _bounded,
    _get_store,
    _resolve_graph_file_paths,
    _shown_of,
    _validate_positive_int,
)

logger = logging.getLogger(__name__)

# Hard ceilings shared by the review tools. All three walk the full impact
# radius of a change set, so on a whole-repo diff every list below is
# proportional to the repository, not to the change. The numbers are set
# from measured cost per row against a 5.6k-node graph:
#   node dict ~60 tok, node+risk_score ~118 tok, test gap ~85 tok,
#   source line ~10 tok, flow with full steps ~980 tok, flow metadata ~18 tok.
_MAX_REVIEW_NODES = 100
_MAX_REVIEW_EDGES = 150
_MAX_REVIEW_FILES = 200
_MAX_REVIEW_SOURCE_LINES = 800
_MAX_LINES_PER_FILE = 500
_MAX_CHANGED_FUNCTIONS = 100
_MAX_DETECT_SOURCE_LINES = 600
_MAX_DETECT_FLOWS = 200

# ``detect_changes`` embeds affected flows for context, not for flow
# spelunking: every flow carries a full ``steps`` list, which is exactly
# what made get_affected_flows return 247k tokens in #849. Callers who want
# step detail should use ``carto flow``, so the embedded copy keeps per-flow
# metadata only.
_DETECT_FLOW_FIELDS = (
    "id", "name", "criticality", "depth", "node_count", "file_count",
)


def _project(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """Keep only *fields* on each row, dropping keys the row does not have."""
    return [{k: r[k] for k in fields if k in r} for r in rows]


# ---------------------------------------------------------------------------
# Tool 4: get_review_context
# ---------------------------------------------------------------------------


def get_review_context(
    changed_files: list[str] | None = None,
    max_depth: int = 2,
    include_source: bool = True,
    max_lines_per_file: int = 200,
    repo_root: str | None = None,
    base: str = "HEAD~1",
    detail_level: str = "standard",
    max_results: int = 50,
    max_files: int = 25,
) -> dict[str, Any]:
    """Generate a focused review context from changed files.

    Builds a token-optimized subgraph + source snippets for code review.

    Args:
        changed_files: Files to review (auto-detected from git diff if omitted).
        max_depth: Impact radius depth (default: 2).
        include_source: Whether to include source code snippets (default: True).
        max_lines_per_file: Max source lines per file in output (default: 200).
        repo_root: Repository root path. Auto-detected if omitted.
        base: Git ref for change detection (default: HEAD~1).
        detail_level: Output detail level.  "standard" returns full context;
            "minimal" returns summary, risk level, changed/impacted file counts,
            top 5 key entity names, test gap count, and next tool suggestions.
            Default: "standard".
        max_results: Maximum graph nodes per list and edges to return
            (default 50; nodes capped at 200, edges at 300). Each list
            carries its untruncated ``*_total`` count.
        max_files: Maximum files to list and to emit source snippets for
            (default 25, capped at 200). A whole-repo diff otherwise inlined
            every tracked file's source. Snippets additionally share an
            800-line budget and ``max_lines_per_file`` is capped at 500.

    Returns:
        Structured review context with subgraph, source snippets, and
        review guidance, plus a ``truncated`` flag.
    """
    _validate_positive_int(max_results, "max_results")
    _validate_positive_int(max_files, "max_files")
    _validate_positive_int(max_lines_per_file, "max_lines_per_file")

    store, root = _get_store(repo_root)
    try:
        # Get impact radius first
        if changed_files is None:
            changed_files = get_changed_files(root, base)
            if not changed_files:
                changed_files = get_staged_and_unstaged(root)

        if not changed_files:
            return {
                "status": "ok",
                "summary": "No changes detected. Nothing to review.",
                "context": {},
            }

        graph_files = _resolve_graph_file_paths(store, root, changed_files)
        original_tokens = estimate_file_tokens(root, changed_files)
        impact = store.get_impact_radius(graph_files, max_depth=max_depth)

        if detail_level == "minimal":
            impacted_count = len(impact["impacted_nodes"])
            if impacted_count > 20:
                risk = "high"
            elif impacted_count > 5:
                risk = "medium"
            else:
                risk = "low"

            key_entities = [
                n.name for n in impact["changed_nodes"][:5]
            ]

            # Count test gaps among changed functions.
            changed_funcs = [
                n for n in impact["changed_nodes"]
                if n.kind == "Function" and not n.is_test
            ]
            test_edges = [
                e for e in impact["edges"] if e.kind == "TESTED_BY"
            ]
            tested_qualified = {e.source_qualified for e in test_edges}
            test_gap_count = sum(
                1 for f in changed_funcs
                if f.qualified_name not in tested_qualified
            )

            summary_parts = [
                f"Review context for {len(changed_files)} changed file(s):",
                f"  - Risk: {risk}",
                f"  - {len(impact['impacted_nodes'])} impacted nodes"
                f" in {len(impact['impacted_files'])} files",
            ]

            result = {
                "status": "ok",
                "summary": "\n".join(summary_parts),
                "risk": risk,
                "changed_file_count": len(changed_files),
                "impacted_file_count": len(impact["impacted_files"]),
                "key_entities": key_entities,
                "test_gaps": test_gap_count,
                "next_tool_suggestions": [
                    "detect_changes",
                    "get_affected_flows",
                    "get_impact_radius",
                ],
            }
            attach_context_savings(result, original_tokens=original_tokens)
            return result

        # Build review context. Every list below scales with the change set,
        # so each is bounded and reports its untruncated total.
        shown_files, files_total, files_cut = _bounded(
            changed_files, max_files, _MAX_REVIEW_FILES,
        )
        impacted_files, impacted_total, impacted_cut = _bounded(
            impact["impacted_files"], max_files, _MAX_REVIEW_FILES,
        )
        changed_nodes, changed_nodes_total, cn_cut = _bounded(
            impact["changed_nodes"], max_results, _MAX_REVIEW_NODES,
        )
        impacted_nodes, impacted_nodes_total, in_cut = _bounded(
            impact["impacted_nodes"], max_results, _MAX_REVIEW_NODES,
        )
        edges, edges_total, edges_cut = _bounded(
            impact["edges"], max_results, _MAX_REVIEW_EDGES,
        )
        truncated = (
            files_cut or impacted_cut or cn_cut or in_cut or edges_cut
        )

        context: dict[str, Any] = {
            "changed_files": shown_files,
            "changed_files_total": files_total,
            "impacted_files": impacted_files,
            "impacted_files_total": impacted_total,
            "graph": {
                "changed_nodes": [node_to_dict(n) for n in changed_nodes],
                "changed_nodes_total": changed_nodes_total,
                "impacted_nodes": [node_to_dict(n) for n in impacted_nodes],
                "impacted_nodes_total": impacted_nodes_total,
                "edges": [edge_to_dict(e) for e in edges],
                "edges_total": edges_total,
            },
            "truncated": truncated,
        }

        # Add source snippets for the bounded file list, spending a shared
        # line budget. Snippets were 109k of a 134k-token worst case: without
        # a total budget, ``max_lines_per_file`` alone lets N files each
        # contribute a whole file.
        if include_source:
            snippets = {}
            per_file = min(max_lines_per_file, _MAX_LINES_PER_FILE)
            budget = _MAX_REVIEW_SOURCE_LINES
            for rel_path in shown_files:
                if budget <= 0:
                    context["source_truncated"] = True
                    context["truncated"] = True
                    break
                full_path = root / rel_path
                if full_path.is_file():
                    try:
                        lines = full_path.read_text(
                            errors="replace"
                        ).splitlines()
                        allowed = min(per_file, budget)
                        if len(lines) > allowed:
                            # Include only the relevant functions/classes
                            relevant_lines = _extract_relevant_lines(
                                lines,
                                impact["changed_nodes"],
                                str(full_path),
                                allowed,
                            )
                            snippets[rel_path] = relevant_lines
                            budget -= allowed
                        else:
                            snippets[rel_path] = "\n".join(
                                f"{i+1}: {line}"
                                for i, line in enumerate(lines)
                            )
                            budget -= len(lines)
                    except (OSError, UnicodeDecodeError):
                        snippets[rel_path] = "(could not read file)"
            context["source_snippets"] = snippets

        # Generate review guidance
        guidance = _generate_review_guidance(impact, changed_files)
        context["review_guidance"] = guidance

        summary_parts = [
            f"Review context for {files_total} changed file(s)"
            + _shown_of(len(shown_files), files_total) + ":",
            f"  - {changed_nodes_total} directly changed nodes"
            + _shown_of(len(changed_nodes), changed_nodes_total),
            f"  - {impacted_nodes_total} impacted nodes"
            f" in {impacted_total} files"
            + _shown_of(len(impacted_nodes), impacted_nodes_total),
            "",
            "Review guidance:",
            guidance,
        ]

        result = {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "context": context,
        }
        attach_context_savings(result, original_tokens=original_tokens)
        return result
    finally:
        store.close()


def _extract_relevant_lines(
    lines: list[str], nodes: list, file_path: str, max_lines: int = 200,
) -> str:
    """Extract only the lines relevant to changed nodes.

    Bounded by *max_lines*: a file where every function changed merges into
    one range covering the whole file, which would defeat the caller's
    ``max_lines_per_file`` budget entirely.
    """
    ranges = []
    for n in nodes:
        if n.file_path == file_path:
            start = max(0, n.line_start - 3)  # 2 lines context before
            end = min(len(lines), n.line_end + 2)  # 1 line context after
            ranges.append((start, end))

    if not ranges:
        # Show first N lines as fallback
        return "\n".join(
            f"{i+1}: {line}" for i, line in enumerate(lines[:min(50, max_lines)])
        )

    # Merge overlapping ranges
    ranges.sort()
    merged = [ranges[0]]
    for start, end in ranges[1:]:
        if start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    parts: list[str] = []
    emitted = 0
    for start, end in merged:
        if emitted >= max_lines:
            parts.append("... (truncated)")
            break
        if parts:
            parts.append("...")
        for i in range(start, min(end, start + max_lines - emitted)):
            parts.append(f"{i+1}: {lines[i]}")
            emitted += 1

    return "\n".join(parts)


def _generate_review_guidance(
    impact: dict, changed_files: list[str]
) -> str:
    """Generate review guidance based on the impact analysis."""
    guidance_parts = []

    # Check for test coverage
    changed_funcs = [
        n for n in impact["changed_nodes"] if n.kind == "Function"
    ]
    test_edges = [e for e in impact["edges"] if e.kind == "TESTED_BY"]
    tested_funcs = {e.source_qualified for e in test_edges}

    untested = [
        f for f in changed_funcs
        if f.qualified_name not in tested_funcs and not f.is_test
    ]
    if untested:
        guidance_parts.append(
            f"- {len(untested)} changed function(s) lack test coverage: "
            + ", ".join(n.name for n in untested[:5])
        )

    # Check for wide blast radius
    if len(impact["impacted_nodes"]) > 20:
        guidance_parts.append(
            f"- Wide blast radius: {len(impact['impacted_nodes'])} "
            "nodes impacted. "
            "Review callers and dependents carefully."
        )

    # Check for inheritance changes
    inheritance_edges = [
        e for e in impact["edges"]
        if e.kind in ("INHERITS", "IMPLEMENTS")
    ]
    if inheritance_edges:
        guidance_parts.append(
            f"- {len(inheritance_edges)} inheritance/implementation "
            "relationship(s) affected. "
            "Check for Liskov substitution violations."
        )

    # Check for cross-file impact
    impacted_file_count = len(impact["impacted_files"])
    if impacted_file_count > 3:
        guidance_parts.append(
            f"- Changes impact {impacted_file_count} other files."
            " Consider splitting into smaller PRs."
        )

    if not guidance_parts:
        guidance_parts.append(
            "- Changes appear well-contained with minimal blast radius."
        )

    return "\n".join(guidance_parts)


# ---------------------------------------------------------------------------
# Tool 16: detect_changes  [REVIEW]
# ---------------------------------------------------------------------------


def detect_changes_func(
    base: str = "HEAD~1",
    changed_files: list[str] | None = None,
    include_source: bool = False,
    max_depth: int = 2,
    repo_root: str | None = None,
    detail_level: str = "standard",
    max_results: int = 25,
    max_flows: int = 20,
) -> dict[str, Any]:
    """Detect changes and produce risk-scored review guidance.

    [REVIEW] Primary tool for code review.  Maps git diffs to affected
    functions, flows, communities, and test coverage gaps.  Returns
    priority-ordered review guidance with risk scores.

    Args:
        base: Git ref to diff against (default: HEAD~1).
        changed_files: Explicit list of changed file paths (relative to repo
            root).  Auto-detected from git diff if omitted.
        include_source: If True, include source code snippets for changed
            functions.  Default: False.
        max_depth: Impact radius depth for BFS traversal.  Default: 2.
        repo_root: Repository root path.  Auto-detected if omitted.
        detail_level: Output detail level.  "standard" returns full analysis;
            "minimal" returns only summary, risk_score, changed_file_count,
            test_gap_count, and top 3 review priorities (text only).
            Default: "standard".
        max_results: Maximum changed functions and test gaps to return
            (default 25, capped at 200). ``changed_functions_total`` and
            ``test_gaps_total`` report the untruncated counts.
        max_flows: Maximum affected flows to embed (default 20, capped at
            200). The embedded flows carry per-flow metadata only; use
            get_affected_flows_tool for step detail. See #849.

    Returns:
        Risk-scored analysis with changed functions, affected flows,
        test gaps, and review priorities, plus ``truncated``.
    """
    _validate_positive_int(max_results, "max_results")
    _validate_positive_int(max_flows, "max_flows")

    store, root = _get_store(repo_root)
    try:
        # Detect changed files if not provided.
        if changed_files is None:
            changed_files = get_changed_files(root, base)
            if not changed_files:
                changed_files = get_staged_and_unstaged(root)

        if not changed_files:
            return {
                "status": "ok",
                "summary": "No changed files detected.",
                "risk_score": 0.0,
                "changed_functions": [],
                "affected_flows": [],
                "test_gaps": [],
                "review_priorities": [],
            }

        original_tokens = estimate_file_tokens(root, changed_files)

        # Convert to absolute paths for graph lookup. Graph identity uses
        # POSIX separators (#774), so normalize the joined paths.
        abs_files = [normalize_file_path(root / f) for f in changed_files]

        # Parse diff ranges for line-level mapping.
        diff_ranges = parse_diff_ranges(str(root), base)
        # Remap to absolute paths so they match graph file_paths.
        abs_ranges: dict[str, list[tuple[int, int]]] = {}
        for rel_path, ranges in diff_ranges.items():
            abs_path = normalize_file_path(root / rel_path)
            abs_ranges[abs_path] = ranges

        analysis = analyze_changes(
            store,
            changed_files=abs_files,
            changed_ranges=abs_ranges if abs_ranges else None,
            repo_root=str(root),
            base=base,
        )

        # Optionally include source snippets for changed functions, spending a
        # shared line budget. Inlining every changed function body turned a
        # whole-repo diff into a 30k-token ``changed_functions`` list.
        if include_source:
            budget = _MAX_DETECT_SOURCE_LINES
            for func in analysis.get("changed_functions", []):
                if budget <= 0:
                    break
                fp = func.get("file_path")
                ls = func.get("line_start")
                le = func.get("line_end")
                if fp and ls and le:
                    file_path = Path(fp)
                    if file_path.is_file():
                        try:
                            lines = file_path.read_text(
                                errors="replace"
                            ).splitlines()
                            start = max(0, ls - 1)
                            end = min(len(lines), le, start + budget)
                            func["source"] = "\n".join(
                                f"{i + 1}: {lines[i]}"
                                for i in range(start, end)
                            )
                            budget -= max(0, end - start)
                        except (OSError, UnicodeDecodeError):
                            func["source"] = "(could not read file)"

        if detail_level == "minimal":
            priorities = analysis.get("review_priorities", [])
            top_priorities = [
                p.get("name", p.get("qualified_name", ""))
                for p in priorities[:3]
            ]
            result: dict[str, Any] = {
                "status": "ok",
                "summary": analysis.get("summary", ""),
                "risk_score": analysis.get("risk_score", 0.0),
                "changed_file_count": len(changed_files),
                "test_gap_count": len(analysis.get("test_gaps", [])),
                "review_priorities": top_priorities,
            }
        else:
            funcs, funcs_total, funcs_cut = _bounded(
                analysis.get("changed_functions", []),
                max_results, _MAX_CHANGED_FUNCTIONS,
            )
            gaps, gaps_total, gaps_cut = _bounded(
                analysis.get("test_gaps", []),
                max_results, _MAX_CHANGED_FUNCTIONS,
            )
            flows, flows_total, flows_cut = _bounded(
                analysis.get("affected_flows", []),
                max_flows, _MAX_DETECT_FLOWS,
            )
            files, files_total, files_cut = _bounded(
                changed_files, max_results, _MAX_REVIEW_FILES,
            )
            any_cut = funcs_cut or gaps_cut or flows_cut or files_cut
            summary = analysis.get("summary", "")
            if any_cut:
                summary += (
                    "\n  - Response bounded: "
                    f"{len(funcs)} of {funcs_total} changed function(s), "
                    f"{len(gaps)} of {gaps_total} test gap(s), "
                    f"{len(flows)} of {flows_total} flow(s) shown"
                )
            result = {
                "status": "ok",
                **analysis,
                "summary": summary,
                "changed_files": files,
                "changed_file_count": files_total,
                "changed_functions": funcs,
                "changed_functions_total": funcs_total,
                "test_gaps": gaps,
                "test_gaps_total": gaps_total,
                "affected_flows": _project(flows, _DETECT_FLOW_FIELDS),
                "affected_flows_total": flows_total,
                "truncated": any_cut,
            }
        result["_hints"] = generate_hints(
            "detect_changes", result, get_session()
        )
        attach_context_savings(result, original_tokens=original_tokens)
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()
