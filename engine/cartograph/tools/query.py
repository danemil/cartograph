"""Tools 2, 3, 5, 6, 9: query / search / stats helpers."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from ..config_keys import normalize_spring_config_key
from ..context_savings import attach_context_savings, estimate_file_tokens
from ..embeddings import EmbeddingStore
from ..compact import display_name
from ..graph import GraphNode, GraphStore, _sanitize_name, edge_to_dict, node_to_dict
from ..hints import generate_hints, get_session
from ..incremental import (
    get_changed_files,
    get_db_path,
    get_staged_and_unstaged,
    is_generated_file,
)
from ..parser import normalize_file_path
from ..repo_paths import graph_path as _graph_path, relativise
from ..search import hybrid_search
from ..uncertainty import (
    empty_impact_confidence,
    empty_query_confidence,
    empty_search_confidence,
)
from ._common import (
    _BUILTIN_CALL_NAMES,
    _get_store,
    _resolve_graph_file_paths,
    _shown_of,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool 2: get_impact_radius
# ---------------------------------------------------------------------------

#: What `large-functions` ranks unless told otherwise. Methods are Function
#: nodes with a parent, so they are included; File and Class nodes are not,
#: because a file or class always outranks the functions inside it and the
#: question asked was about functions. Test functions and methods are Test
#: nodes, and they are functions too: a user's reference top 10 held two test
#: methods that the Function-only default could never return.
LARGE_DEFAULT_KINDS = ("Function", "Test")

#: Test nodes that group other tests — JS/TS ``describe``/``suite`` blocks —
#: rather than being one. They wrap a whole spec file the way a class wraps its
#: methods, and on claude-mem they held 18 of the 20 largest Test nodes. Left
#: out of the default only; ``--kind Test`` lists them.
_SUITE_BLOCK = re.compile(r"^(describe|suite)(:|@L)")

_QUERY_PATTERNS = {
    "callers_of": "Find all functions that call a given function",
    "references_to": "Find all nodes that reference a given symbol",
    "callees_of": "Find all functions called by a given function",
    "imports_of": "Find all imports of a given file or module",
    "importers_of": "Find all files that import a given file or module",
    "children_of": "Find all nodes contained in a file or class",
    "tests_for": "Find all tests for a given function or class",
    "inheritors_of": "Find all classes that inherit from a given class",
    "triggers_of": "Find methods invoked by a scheduler or other trigger",
    "triggered_by": "Find schedulers or other triggers that invoke a method",
    "publishers_of": "Find methods that publish an event",
    "listeners_of": "Find methods that listen for an event",
    "handlers_of": "Find methods that handle an endpoint",
    "endpoints_for": "Find endpoints handled by a method",
    "consumers_of": "Find classes that consume a Spring configuration property",
    "file_summary": "Get a summary of all nodes in a file",
}

_JAVA_FQN_PART = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_MAX_FQN_CANDIDATES = 100


def _looks_like_java_method_fqn(target: str) -> bool:
    """Return whether *target* has a package/Class/method-like shape."""
    if "::" in target:
        return False
    parts = target.split(".")
    if len(parts) < 2 or not all(_JAVA_FQN_PART.fullmatch(part) for part in parts):
        return False
    # Two segments are accepted only for the conventional Class.method form;
    # this keeps ordinary dotted filenames/modules on the legacy path.
    return len(parts) >= 3 or parts[-2][:1].isupper()


def _java_fqn_candidates(store: GraphStore, target: str) -> list[GraphNode] | None:
    """Resolve Java FQNs using language plus class/file evidence.

    ``None`` means that the target is not Java-FQN-shaped. An empty list means
    it is shaped like one but no safe match exists, so callers must not fall
    back to an unrelated globally unique method name.
    """
    if not _looks_like_java_method_fqn(target):
        return None

    parts = target.split(".")
    class_name, method_name = parts[-2:]
    matches: list[GraphNode] = []
    for candidate in store.search_nodes(method_name, limit=_MAX_FQN_CANDIDATES):
        if candidate.language.lower() != "java" or candidate.name != method_name:
            continue
        parent_name = candidate.parent_name or ""
        parent_match = parent_name.rsplit(".", 1)[-1] == class_name
        file_match = Path(candidate.file_path).stem == class_name
        qualified_tail = candidate.qualified_name.rsplit("::", 1)[-1]
        qualified_match = qualified_tail.endswith(f"{class_name}.{method_name}")
        if parent_match or file_match or qualified_match:
            matches.append(candidate)
    return matches


def _rank_disambiguation_candidates(
    candidates: list[GraphNode], target: str,
) -> list[dict[str, Any]]:
    """Return deterministic, sanitized candidates ordered by match quality."""
    target_lower = target.lower()

    def score(node: GraphNode) -> tuple[int, str]:
        if node.qualified_name == target:
            rank = 0
        elif node.name == target:
            rank = 1
        elif target_lower in node.qualified_name.lower():
            rank = 2
        else:
            rank = 3
        return rank, node.qualified_name

    return [node_to_dict(node) for node in sorted(candidates, key=score)]


#: How a dependent depends on a changed node, as the verb its row leads with.
_RELATION_VERBS = {
    "CALLS": "calls", "IMPORTS_FROM": "imports", "INHERITS": "inherits",
    "IMPLEMENTS": "implements", "OVERRIDES": "overrides", "TESTED_BY": "tests",
    "REFERENCES": "references", "DEPENDS_ON": "depends on",
}
#: The same, counted in the summary: "(9 direct: 4 call, 5 import only)".
_RELATION_NOUNS = {
    "CALLS": "call", "INHERITS": "inherit", "IMPLEMENTS": "implement",
    "OVERRIDES": "override", "TESTED_BY": "test", "REFERENCES": "reference",
    "DEPENDS_ON": "depend", "IMPORTS_FROM": "import only", "OTHER": "other",
}
#: Names per verb in one row before the rest are counted.
_MAX_VIA_NAMES = 3


def _short_name(qualified: str) -> str:
    """``path/x.py::Store.add`` -> ``Store.add``; a file -> its base name."""
    if "::" in qualified:
        return qualified.rsplit("::", 1)[1]
    return Path(qualified.replace("\\", "/")).name or qualified


def _via(links: list[tuple[str, str]]) -> list[str]:
    """``["calls add_node, get_node", "tests add_node"]``, strongest relation first.

    Names only, no paths: a direct dependent's names are the changed file's
    own symbols, and a transitive one's is the hop the row's own path follows.
    """
    by_verb: dict[str, list[str]] = {}
    for kind, other in links:
        verb = _RELATION_VERBS.get(kind, kind.lower().replace("_", " "))
        names = by_verb.setdefault(verb, [])
        name = _short_name(other)
        if name not in names:
            names.append(name)
    out = []
    for verb, names in by_verb.items():
        text = f"{verb} " + ", ".join(names[:_MAX_VIA_NAMES])
        if len(names) > _MAX_VIA_NAMES:
            text += f", +{len(names) - _MAX_VIA_NAMES} more"
        out.append(text)
    return out


def _impact_summary(
    changed_files: list[str], max_depth: int, totals: dict[str, Any],
    *, shown: int, shown_direct: int,
) -> str:
    """One line that states the whole scope, and how much of it is listed.

    Read first and sometimes read alone, so it must not let a short list pass
    for the answer: the totals are exact, and when direct dependents outnumber
    the rows it says so, with the limit that lists every one of them — direct
    dependents rank first, so ``--limit <direct>`` is exactly that set.
    """
    label = (
        Path(changed_files[0]).name if len(changed_files) == 1
        else f"{len(changed_files)} changed files"
    )
    items, direct, files = totals["items"], totals["direct"], totals["files"]
    if not items:
        return (
            f"{label}: nothing affected within {max_depth} hops "
            f"({totals['changed_nodes']} nodes in the changed files)"
        )
    by_relation = totals.get("direct_by_relation") or {}
    split = (": " + ", ".join(f"{n} {r}" for r, n in by_relation.items())
             if by_relation else "")
    scope = (
        f"{label}: {items} items affected within {max_depth} hops across "
        f"{files} files ({direct} direct{split})"
    )
    if shown >= items:
        line = f"{scope}; all shown"
    else:
        line = f"{scope}; showing top {shown}"
        if shown_direct < direct:
            line += (
                f"; {direct} direct dependents; {shown_direct} shown; "
                f"--limit {direct} lists them all"
            )
    # Direct means one hop, and a hop can be a call or only an import. Read
    # alike, importers were reported as broken by a signature change (report6,
    # T5); the summary is the line read first, so it says which is which.
    imports_only = by_relation.get(_RELATION_NOUNS["IMPORTS_FROM"], 0)
    if imports_only:
        line += (
            f"; the {imports_only} import-only dependents are not broken by a "
            "signature change, only by a rename or removal"
        )
    if len(changed_files) == 1:
        line += (
            f"; callers of one symbol: carto query callers_of "
            f"{changed_files[0]}::<name>"
        )
    return line


def get_impact_radius(
    changed_files: list[str] | None = None,
    max_depth: int = 2,
    max_results: int = 500,
    repo_root: str | None = None,
    base: str = "HEAD~1",
    detail_level: str = "standard",
) -> dict[str, Any]:
    """Analyze the blast radius of changed files.

    Args:
        changed_files: Explicit list of changed file paths (relative to repo root).
                       If omitted, auto-detects from git diff.
        max_depth: How many hops to traverse in the graph (default: 2).
        max_results: Maximum impacted nodes to return (default: 500). Totals,
            ``affected_files`` and ``impacted_files`` cover every impacted
            node regardless.
        repo_root: Repository root path. Auto-detected if omitted.
        base: Git ref for auto-detecting changes (default: HEAD~1).
        detail_level: "standard" (full output) or "minimal" (summary only).

    Returns:
        Changed nodes, impacted nodes (direct first, then by impact score),
        every impacted file with its counts, connecting edges, exact
        ``totals``, plus ``truncated`` flag and ``total_impacted`` count.
    """
    if isinstance(max_results, bool) or max_results < 1:
        raise ValueError("max_results must be an integer greater than or equal to 1")

    store, root = _get_store(repo_root)
    try:
        if changed_files is None:
            changed_files = get_changed_files(root, base)
            if not changed_files:
                changed_files = get_staged_and_unstaged(root)

        if not changed_files:
            return {
                "status": "ok",
                "summary": "No changed files detected.",
                "totals": {
                    "items": 0, "direct": 0, "files": 0,
                    "changed_nodes": 0, "edges": {},
                },
                "changed_nodes": [],
                "impacted_nodes": [],
                "impacted_files": [],
                "truncated": False,
                "total_impacted": 0,
            }

        # Resolve user-facing paths to the file paths stored in the graph.
        original_tokens = estimate_file_tokens(root, changed_files)
        abs_files = _resolve_graph_file_paths(store, root, changed_files)
        result = store.get_impact_radius(
            abs_files, max_depth=max_depth, max_nodes=max_results,
            relations=True,
        )
        relations = result.get("relations") or {}
        via = relations.get("via", {})

        impact_scores = result.get("impact_scores", {})
        direct_qns = result.get("direct_qns", set())
        changed_dicts = [node_to_dict(n) for n in result["changed_nodes"]]
        impacted_dicts = []
        for node in result["impacted_nodes"]:
            node_dict = node_to_dict(node)
            score = impact_scores.get(node.qualified_name)
            if score is not None:
                node_dict["impact_score"] = score
            node_dict["direct"] = node.qualified_name in direct_qns
            if via.get(node.qualified_name):
                node_dict["via"] = _via(via[node.qualified_name])
            impacted_dicts.append(node_dict)
        edge_dicts = [edge_to_dict(e) for e in result["edges"]]
        truncated = result["truncated"]
        total_impacted = result["total_impacted"]
        # Files with a direct dependent first, then by how much of each file is
        # affected: the order in which they are worth opening.
        file_counts = sorted(
            result.get("file_counts", []), key=lambda f: (-f[2], -f[1], f[0]),
        )
        totals = {
            "items": total_impacted,
            "direct": len(direct_qns),
            "files": len(file_counts),
            "changed_nodes": len(changed_dicts),
            "edges": result.get("edge_counts", {}),
        }
        if relations.get("direct"):
            # Strongest relation first, the order the traversal weighs them.
            order = list(_RELATION_NOUNS)
            totals["direct_by_relation"] = {
                _RELATION_NOUNS.get(kind, kind.lower()): n
                for kind, n in sorted(
                    relations["direct"].items(),
                    key=lambda item: (order.index(item[0]) if item[0] in order
                                      else len(order), item[0]),
                )
            }
        import_only_files = relations.get("import_only_files", {})
        summary = _impact_summary(
            changed_files, max_depth, totals, shown=len(impacted_dicts),
            shown_direct=sum(1 for n in impacted_dicts if n["direct"]),
        )

        # "Nothing is impacted" and "nothing about these files is indexed"
        # look identical to a reader without this marker.
        confidence = None
        if not impacted_dicts:
            changed_language = next(
                (n.language for n in result["changed_nodes"] if n.language), None,
            )
            confidence = empty_impact_confidence(
                store, root, changed_files, abs_files, changed_language,
            )

        if detail_level == "minimal":
            impacted_count = len(impacted_dicts)
            if impacted_count > 20:
                risk = "high"
            elif impacted_count > 5:
                risk = "medium"
            else:
                risk = "low"
            key_entities = [
                n["name"] for n in impacted_dicts[:5]
            ]
            minimal_response = {
                "status": "ok",
                "summary": summary,
                "risk": risk,
                "impacted_file_count": totals["files"],
                "key_entities": key_entities,
                "truncated": truncated,
                "nodes_omitted": max(0, total_impacted - len(impacted_dicts)),
            }
            if confidence:
                minimal_response["confidence"] = confidence
            attach_context_savings(minimal_response, original_tokens=original_tokens)
            return minimal_response

        response: dict[str, Any] = {
            "status": "ok",
            "summary": summary,
            "totals": totals,
            "changed_files": changed_files,
            "changed_nodes": changed_dicts,
            "impacted_nodes": impacted_dicts,
            # Every affected file, whatever the cap on impacted_nodes, so the
            # scope of a change is never shortened along with its list.
            "impacted_files": [path for path, _, _ in file_counts],
            "affected_files": [
                {"file": path, "items": items, "direct": direct,
                 **({"import_only": import_only_files[path]}
                    if import_only_files.get(path) else {})}
                for path, items, direct in file_counts
            ],
            "edges": edge_dicts,
            "truncated": truncated,
            "total_impacted": total_impacted,
            "nodes_omitted": max(0, total_impacted - len(impacted_dicts)),
        }
        if confidence:
            response["confidence"] = confidence
        attach_context_savings(response, original_tokens=original_tokens)
        return response
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 3: query_graph
# ---------------------------------------------------------------------------


def query_graph(
    pattern: str,
    target: str,
    repo_root: str | None = None,
    detail_level: str = "standard",
    max_results: int = 100,
) -> dict[str, Any]:
    """Run a predefined graph query.

    Args:
        pattern: Query pattern. One of: callers_of, references_to, callees_of,
                 imports_of, importers_of, children_of, tests_for, inheritors_of,
                 triggers_of, triggered_by, publishers_of, listeners_of,
                 handlers_of, endpoints_for, consumers_of, file_summary.
        target: The node name, qualified name, or file path to query about.
        repo_root: Repository root path. Auto-detected if omitted.
        detail_level: "standard" (full output) or "minimal" (summary only).
        max_results: Maximum results to return. Minimal mode additionally caps
            visible results at five and reports the exact omitted count.

    Returns:
        Matching nodes and their aligned edges, with total and omitted counts.
    """
    if isinstance(max_results, bool) or max_results < 1:
        raise ValueError("max_results must be an integer greater than or equal to 1")

    store, root = _get_store(repo_root)
    try:
        if pattern not in _QUERY_PATTERNS:
            return {
                "status": "error",
                "error": (
                    f"Unknown pattern '{pattern}'. "
                    f"Available: {list(_QUERY_PATTERNS.keys())}"
                ),
            }

        response_limit = min(max_results, 5) if detail_level == "minimal" else max_results
        results: list[dict[str, Any]] = []
        edges_out: list[dict[str, Any]] = []
        total_results = 0

        def add_result(result: dict[str, Any], edge: Any | None = None) -> None:
            """Count every logical result but retain only the bounded prefix."""
            nonlocal total_results
            total_results += 1
            if len(results) >= response_limit:
                return
            results.append(result)
            if edge is not None:
                edges_out.append(edge_to_dict(edge))

        # For callers_of, skip common builtins early (bare names only)
        # "Who calls .map()?" returns hundreds of useless hits.
        # Qualified names (e.g. "utils.py::map") bypass this filter.
        if (
            pattern == "callers_of"
            and target in _BUILTIN_CALL_NAMES
            and "::" not in target
        ):
            return {
                "status": "ok", "pattern": pattern, "target": target,
                "description": _QUERY_PATTERNS[pattern],
                "summary": (
                    f"'{target}' is a common builtin "
                    "— callers_of skipped to avoid noise."
                ),
                "result_count": 0,
                "results_omitted": 0,
                "results": [], "edges": [],
            }

        # Resolve target - try as-is, then as absolute path, then search.
        # file_summary targets are paths, so skip broad node search.
        node = None
        raw_config_target = pattern == "consumers_of" and "::" not in target
        if pattern != "file_summary" and not raw_config_target:
            node = store.get_node(target)
            if not node:
                abs_target = _graph_path(target, root)
                node = store.get_node(abs_target)
            if not node:
                java_candidates = _java_fqn_candidates(store, target)
                candidates = (
                    java_candidates
                    if java_candidates is not None
                    else store.search_nodes(target, limit=20)
                )
                if pattern == "inheritors_of" and "::" not in target:
                    exact_type_candidates = [
                        candidate
                        for candidate in candidates
                        if candidate.name == target
                        and candidate.kind
                        in {"Class", "Interface", "Type", "Struct", "Enum", "Trait"}
                    ]
                    if exact_type_candidates:
                        candidates = exact_type_candidates
                if len(candidates) == 1:
                    node = candidates[0]
                    target = node.qualified_name
                elif len(candidates) > 1:
                    candidate_count = (
                        len(candidates)
                        if java_candidates is not None
                        else store.count_search_nodes(target)
                    )
                    ranked = _rank_disambiguation_candidates(candidates, target)
                    return {
                        "status": "ambiguous",
                        "summary": (
                            f"'{target}' matches {candidate_count} node(s). "
                            "Re-run with a qualified_name from disambiguation."
                        ),
                        # Preserve the established key while adding the clearer
                        # agent-facing name introduced by #458.
                        "candidates": ranked,
                        "disambiguation": ranked,
                        "candidate_count": candidate_count,
                        "candidates_truncated": candidate_count > len(candidates),
                        "hint": (
                            "Use a qualified_name from disambiguation as the "
                            "target parameter."
                        ),
                    }

        if not node and pattern not in ("consumers_of", "file_summary"):
            # This branch, not the empty-result path below, is where an
            # unresolved target actually lands for most patterns, so the
            # not-indexed marker has to be attached here too.
            unresolved: dict[str, Any] = {
                "status": "not_found",
                "summary": f"No node found matching '{target}'.",
            }
            unresolved_note = empty_query_confidence(store, root, pattern, target, None)
            if unresolved_note:
                unresolved["confidence"] = unresolved_note
            return unresolved

        qn = node.qualified_name if node else target

        if pattern == "callers_of":
            seen_sources: set[str] = set()
            for e in store.iter_edges_by_target(qn):
                if e.kind == "CALLS":
                    if e.source_qualified not in seen_sources:
                        seen_sources.add(e.source_qualified)
                        caller = store.get_node(e.source_qualified)
                        if caller:
                            add_result(node_to_dict(caller), e)
            # Fallback: CALLS edges store unqualified target names
            # (e.g. "generateTestCode") while qn is fully qualified
            # (e.g. "file.ts::generateTestCode"). Search by plain name too.
            if node:
                cpp_overload_count = (
                    store.count_nodes_by_name(
                        node.name,
                        language="cpp",
                        kinds=("Function", "Test"),
                    )
                    if node.language == "cpp"
                    else 0
                )
                for e in store.iter_edges_by_target_name(
                    node.name,
                    language=node.language or None,
                ):
                    # A C++ overload set deliberately keeps the target bare.
                    # Its candidates support disambiguation, but do not prove
                    # that any one exact overload was called.
                    if (
                        "ambiguous_targets" in e.extra
                        or "unresolved_targets" in e.extra
                        or (node.language == "cpp" and e.extra.get("receiver"))
                    ):
                        continue
                    if cpp_overload_count > 1:
                        continue
                    if e.source_qualified not in seen_sources:
                        seen_sources.add(e.source_qualified)
                        caller = store.get_node(e.source_qualified)
                        if caller:
                            caller_result = node_to_dict(caller)
                            caller_result["target_resolution"] = "unresolved"
                            add_result(caller_result, e)

        elif pattern == "references_to":
            seen_reference_sources: set[str] = set()
            for e in store.iter_edges_by_target(qn):
                if (
                    e.kind != "REFERENCES"
                    or e.source_qualified in seen_reference_sources
                ):
                    continue
                source = store.get_node(e.source_qualified)
                if source:
                    seen_reference_sources.add(e.source_qualified)
                    add_result(node_to_dict(source), e)

        elif pattern == "callees_of":
            seen_targets: set[str] = set()
            for e in store.iter_edges_by_source(qn):
                if e.kind == "CALLS":
                    if e.target_qualified not in seen_targets:
                        seen_targets.add(e.target_qualified)
                        callee = store.get_node(e.target_qualified)
                        if callee:
                            add_result(node_to_dict(callee), e)
                        elif (
                            isinstance(e.extra.get("ambiguous_targets"), list)
                            or isinstance(e.extra.get("unresolved_targets"), list)
                            or "::" not in e.target_qualified
                            or (node is not None and node.language == "cpp")
                        ):
                            unresolved = (
                                e.extra.get("ambiguous_targets")
                                or e.extra.get("unresolved_targets")
                            )
                            result: dict[str, Any] = {
                                "kind": "Function",
                                "name": e.target_qualified,
                                "qualified_name": e.target_qualified,
                            }
                            if isinstance(unresolved, list):
                                resolution = (
                                    "ambiguous"
                                    if e.extra.get("ambiguous_targets")
                                    else "unresolved"
                                )
                                result["resolution"] = resolution
                                result["candidates"] = [
                                    _sanitize_name(candidate)
                                    for candidate in unresolved[:20]
                                    if isinstance(candidate, str)
                                ]
                                candidate_count = e.extra.get(
                                    f"{resolution}_target_count",
                                )
                                if not isinstance(candidate_count, int):
                                    candidate_count = len(unresolved)
                                result["candidate_count"] = candidate_count
                                result["candidates_truncated"] = bool(
                                    e.extra.get(
                                        f"{resolution}_targets_truncated",
                                    )
                                    or candidate_count > len(result["candidates"])
                                )
                            add_result(result, e)

        elif pattern == "imports_of":
            for e in store.iter_edges_by_source(qn):
                if e.kind == "IMPORTS_FROM":
                    add_result({"import_target": e.target_qualified}, e)

        elif pattern == "importers_of":
            # Find edges where target matches this file.
            # Use resolve() to canonicalize the path, matching how
            # _resolve_module_to_file stores edge targets.
            abs_target = (
                str((root / target).resolve()) if node is None
                else node.file_path
            )
            seen_importers: set[str] = set()
            for e in store.iter_edges_by_target(abs_target):
                if e.kind == "IMPORTS_FROM":
                    if e.source_qualified in seen_importers:
                        continue
                    seen_importers.add(e.source_qualified)
                    add_result({
                        "importer": e.source_qualified,
                        "file": e.file_path,
                    }, e)
            # C# fallback: `using X.Y;` directives produce IMPORTS_FROM edges
            # whose target is the raw namespace string, not a file path, so
            # the path lookup above misses them. Resolve the target file's
            # declared namespace(s) and also search edges by namespace.
            # See: #310
            if node is not None and node.language == "csharp":
                declared_ns: list[str] = []
                for n in store.iter_nodes_by_file(node.file_path):
                    if n.kind == "File":
                        declared_ns = list(
                            n.extra.get("csharp_namespaces", []) or []
                        )
                        break
                for ns in declared_ns:
                    for e in store.iter_edges_by_target(ns):
                        if e.kind != "IMPORTS_FROM":
                            continue
                        if e.source_qualified in seen_importers:
                            continue
                        seen_importers.add(e.source_qualified)
                        add_result({
                            "importer": e.source_qualified,
                            "file": e.file_path,
                        }, e)

        elif pattern == "children_of":
            for e in store.iter_edges_by_source(qn):
                if e.kind == "CONTAINS":
                    child = store.get_node(e.target_qualified)
                    if child:
                        add_result(node_to_dict(child))

        elif pattern == "tests_for":
            # Keep the normal sanitized node response while adding the
            # direct/indirect marker returned by the bounded store lookup.
            seen: set[str] = set()
            for match in store.get_transitive_tests(qn):
                test_qn = match.get("qualified_name")
                if not isinstance(test_qn, str) or test_qn in seen:
                    continue
                test = store.get_node(test_qn)
                if test:
                    result = node_to_dict(test)
                    result["indirect"] = bool(match.get("indirect", False))
                    if match.get("via"):
                        result["via"] = match["via"]
                    add_result(result)
                    seen.add(test_qn)
            # Also search by naming convention
            name = node.name if node else target
            cpp_overload_set = bool(
                node
                and node.language == "cpp"
                and store.count_nodes_by_name(
                    node.name,
                    language="cpp",
                    kinds=("Function", "Test"),
                ) > 1
            )
            test_nodes = []
            if not cpp_overload_set:
                test_nodes = store.search_nodes(f"test_{name}", limit=10)
                test_nodes += store.search_nodes(f"Test{name}", limit=10)
            for t in test_nodes:
                if t.qualified_name not in seen and t.is_test:
                    result = node_to_dict(t)
                    result["indirect"] = False
                    result["inferred_by"] = "naming_convention"
                    add_result(result)
                    seen.add(t.qualified_name)

        elif pattern == "inheritors_of":
            for e in store.iter_edges_by_target(qn):
                if e.kind in ("INHERITS", "IMPLEMENTS"):
                    child = store.get_node(e.source_qualified)
                    if child:
                        add_result(node_to_dict(child), e)
            # Fallback: INHERITS/IMPLEMENTS edges store unqualified base names
            # (e.g. "Animal") while qn is fully qualified
            # (e.g. "sample.dart::Animal"). Search by plain name too. See: #87
            if total_results == 0 and node:
                for kind in ("INHERITS", "IMPLEMENTS"):
                    for e in store.iter_edges_by_target_name(
                        node.name, kind=kind, language=node.language or None,
                    ):
                        child = store.get_node(e.source_qualified)
                        if child:
                            add_result(node_to_dict(child), e)

        elif pattern == "triggers_of":
            for edge in store.get_edges_by_source(qn):
                if edge.kind != "TRIGGERS":
                    continue
                triggered = store.get_node(edge.target_qualified)
                if triggered:
                    add_result(node_to_dict(triggered), edge)
                else:
                    edges_out.append(edge_to_dict(edge))

        elif pattern == "triggered_by":
            for edge in store.get_edges_by_target(qn):
                if edge.kind != "TRIGGERS":
                    continue
                trigger = store.get_node(edge.source_qualified)
                if trigger:
                    add_result(node_to_dict(trigger), edge)
                else:
                    edges_out.append(edge_to_dict(edge))

        elif pattern in ("publishers_of", "listeners_of"):
            edge_kind = "PUBLISHES" if pattern == "publishers_of" else "HANDLES"
            for edge in store.get_edges_by_target(qn):
                if edge.kind != edge_kind:
                    continue
                source = store.get_node(edge.source_qualified)
                if source:
                    add_result(node_to_dict(source), edge)
                else:
                    edges_out.append(edge_to_dict(edge))

        elif pattern == "handlers_of":
            for edge in store.get_edges_by_target(qn):
                if edge.kind != "HANDLES":
                    continue
                handler = store.get_node(edge.source_qualified)
                if handler:
                    add_result(node_to_dict(handler), edge)
                else:
                    edges_out.append(edge_to_dict(edge))

        elif pattern == "endpoints_for":
            for edge in store.get_edges_by_source(qn):
                if edge.kind != "HANDLES":
                    continue
                endpoint = store.get_node(edge.target_qualified)
                if endpoint and endpoint.kind == "Endpoint":
                    add_result(node_to_dict(endpoint), edge)
                elif endpoint is None:
                    edges_out.append(edge_to_dict(edge))

        elif pattern == "consumers_of":
            raw_key = node.name if node else target.removeprefix("config:")
            raw_key = raw_key.removesuffix(".*")
            key = normalize_spring_config_key(raw_key)
            seen_config_sources: set[str] = set()
            for edge in store.get_config_consumers(key):
                consumer = store.get_node(edge.source_qualified)
                if consumer and consumer.qualified_name not in seen_config_sources:
                    add_result(node_to_dict(consumer), edge)
                    seen_config_sources.add(consumer.qualified_name)
                elif consumer is None:
                    edges_out.append(edge_to_dict(edge))

        elif pattern == "file_summary":
            graph_paths = _resolve_graph_file_paths(store, root, [target])
            for graph_path in graph_paths:
                for n in store.iter_nodes_by_file(graph_path):
                    add_result(node_to_dict(n))

        results_omitted = max(0, total_results - len(results))
        summary = (
            f"Found {total_results} result(s) "
            f"for {pattern}('{target}')"
        )
        if results_omitted:
            summary += f" — showing {len(results)}, {results_omitted} omitted"

        # A zero here is the dangerous direction: agents read it as "none
        # exist" and either conclude wrongly or fall back to grepping the
        # repository. One capped sentence prevents both, and is attached only
        # when the result set is empty so non-empty responses are unchanged.
        confidence = (
            empty_query_confidence(store, root, pattern, target, node)
            if total_results == 0
            else None
        )

        if detail_level == "minimal":
            minimal_results = [
                {
                    k: r[k]
                    for k in ("name", "kind", "file_path", "indirect")
                    if k in r
                }
                for r in results
            ]
            minimal_response: dict[str, Any] = {
                "status": "ok",
                "pattern": pattern,
                "target": target,
                "description": _QUERY_PATTERNS[pattern],
                "summary": summary,
                "result_count": total_results,
                "results_omitted": results_omitted,
                "results": minimal_results,
            }
            if confidence:
                minimal_response["confidence"] = confidence
            return minimal_response

        response: dict[str, Any] = {
            "status": "ok",
            "pattern": pattern,
            "target": target,
            "description": _QUERY_PATTERNS[pattern],
            "summary": summary,
            "result_count": total_results,
            "results_omitted": results_omitted,
            "results": results,
            "edges": edges_out,
        }
        if confidence:
            response["confidence"] = confidence
        return response
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 5: semantic_search_nodes
# ---------------------------------------------------------------------------


def semantic_search_nodes(
    query: str,
    kind: str | None = None,
    limit: int = 20,
    repo_root: str | None = None,
    context_files: list[str] | None = None,
    model: str | None = None,
    provider: str | None = None,
    detail_level: str = "standard",
) -> dict[str, Any]:
    """Search for nodes by name, keyword, or semantic similarity.

    Uses hybrid search (FTS5 BM25 + vector embeddings merged via Reciprocal
    Rank Fusion) as the primary search path, with graceful fallback to
    keyword matching.

    Args:
        query: Search string to match against node names and qualified names.
        kind: Optional filter by node kind (File, Class, Function, Type, Test).
        limit: Maximum results to return (default: 20).
        repo_root: Repository root path. Auto-detected if omitted.
        context_files: Optional list of file paths. Nodes in these files
            receive a relevance boost.
        detail_level: "standard" (full output) or "minimal" (summary only).

    Returns:
        Ranked list of matching nodes.
    """
    store, root = _get_store(repo_root)
    try:
        mode_out: list[str] = []
        results = hybrid_search(
            store, query, kind=kind, limit=limit, context_files=context_files,
            model=model, provider=provider, _out_mode=mode_out,
        )

        search_mode = mode_out[0] if mode_out else "keyword"

        summary = f"Found {len(results)} node(s) matching '{query}'" + (
            f" (kind={kind})" if kind else ""
        )

        # Zero hits can mean "no such symbol" or "never indexed"/"stale index";
        # only the marker distinguishes them.
        confidence = (
            empty_search_confidence(store, root, query) if not results else None
        )

        if detail_level == "minimal":
            minimal_results = [
                {
                    k: r[k]
                    for k in ("name", "kind", "file_path", "score")
                    if k in r
                }
                for r in results[:5]
            ]
            minimal_response: dict[str, Any] = {
                "status": "ok",
                "query": query,
                "search_mode": search_mode,
                "summary": summary,
                "results": minimal_results,
                "result_count": len(results),
                "results_omitted": max(0, len(results) - len(minimal_results)),
            }
            if confidence:
                minimal_response["confidence"] = confidence
            return minimal_response

        result: dict[str, object] = {
            "status": "ok",
            "query": query,
            "search_mode": search_mode,
            "summary": summary,
            "results": results,
        }
        if confidence:
            result["confidence"] = confidence
        result["_hints"] = generate_hints(
            "semantic_search_nodes", result, get_session()
        )
        return result
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 6: list_graph_stats
# ---------------------------------------------------------------------------


def list_graph_stats(repo_root: str | None = None) -> dict[str, Any]:
    """Get aggregate statistics about the knowledge graph.

    Args:
        repo_root: Repository root path. Auto-detected if omitted.

    Returns:
        Total nodes, edges, breakdown by kind, languages, and last update time.
    """
    store, root = _get_store(repo_root)
    try:
        stats = store.get_stats()

        summary_parts = [
            f"Graph statistics for {root.name}:",
            f"  Files: {stats.files_count}",
            f"  Total nodes: {stats.total_nodes}",
            f"  Total edges: {stats.total_edges}",
            f"  Languages: {', '.join(stats.languages) if stats.languages else 'none'}",
            f"  Last updated: {stats.last_updated or 'never'}",
            "",
            "Nodes by kind:",
        ]
        for kind, count in sorted(stats.nodes_by_kind.items()):
            summary_parts.append(f"  {kind}: {count}")
        summary_parts.append("")
        summary_parts.append("Edges by kind:")
        for kind, count in sorted(stats.edges_by_kind.items()):
            summary_parts.append(f"  {kind}: {count}")

        # Add embedding info if available
        emb_store = EmbeddingStore(get_db_path(root))
        try:
            emb_count = emb_store.count()
            summary_parts.append("")
            summary_parts.append(f"Embeddings: {emb_count} nodes embedded")
            if not emb_store.available:
                summary_parts.append(
                    "  (install sentence-transformers for semantic search)"
                )
        finally:
            emb_store.close()

        return {
            "status": "ok",
            "summary": "\n".join(summary_parts),
            "total_nodes": stats.total_nodes,
            "total_edges": stats.total_edges,
            "nodes_by_kind": stats.nodes_by_kind,
            "edges_by_kind": stats.edges_by_kind,
            "languages": stats.languages,
            "files_count": stats.files_count,
            "last_updated": stats.last_updated,
            "embeddings_count": emb_count,
        }
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Tool 9: find_large_functions
# ---------------------------------------------------------------------------


def find_large_functions(
    min_lines: int | None = None,
    kind: str | list[str] | tuple[str, ...] | None = None,
    file_path_pattern: str | None = None,
    limit: int = 20,
    repo_root: str | None = None,
    include_generated: bool = False,
) -> dict[str, Any]:
    """The largest functions (methods included), largest first.

    Useful for identifying decomposition targets, code-quality audits,
    and enforcing size limits during code review.

    Args:
        min_lines: Only nodes at least this long. None (default) applies no
            threshold, so ``limit`` alone decides how many come back: a
            "top 10" is ten rows whenever there are ten functions.
        kind: Node kind or kinds to rank: Function, Class, File, Test, Type.
            Defaults to Function and Test, which cover methods and test
            functions — the graph has no separate Method kind; a method is a
            Function with a parent. The default leaves out describe/suite
            blocks, which group tests rather than being one.
        file_path_pattern: Filter by file path substring (e.g. "components/").
        limit: Maximum results (default: 20).
        repo_root: Repository root path. Auto-detected if omitted.
        include_generated: Rank generated, vendored and declaration files too.

    Returns:
        Nodes with line counts, ordered largest first, and ``matching``: how
        many qualified before the limit.
    """
    default_kinds = not kind
    kinds = [kind] if isinstance(kind, str) else list(kind or LARGE_DEFAULT_KINDS)
    store, root = _get_store(repo_root)
    try:
        # Uncapped, because the cap applies after exclusion: capping first
        # would return a short page whenever a generated file ranked high,
        # and could not say how many were set aside.
        nodes = store.get_nodes_by_size(
            min_lines=min_lines if min_lines is not None else 1,
            kind=kinds,
            file_path_pattern=file_path_pattern,
            limit=None,
        )

        results = []
        excluded = 0
        matching = 0
        for n in nodes:
            if default_kinds and n.kind == "Test" and _SUITE_BLOCK.match(n.name):
                continue
            rel = relativise(n.file_path, root)
            if not include_generated and is_generated_file(rel):
                excluded += 1
                continue
            matching += 1
            if len(results) >= limit:
                continue
            d = node_to_dict(n)
            d["line_count"] = (
                (n.line_end - n.line_start + 1)
                if n.line_start and n.line_end
                else 0
            )
            # Make file_path relative for readability. POSIX-shaped, like the
            # rest of graph identity — see parser.normalize_file_path.
            d["relative_path"] = rel
            results.append(d)

        if kinds == ["Function"]:
            noun = "functions"
        elif sorted(kinds) == ["Function", "Test"]:
            # Said, so a row labelled Test is not read as a stray.
            noun = "functions (tests included)"
        else:
            noun = "/".join(kinds) + " nodes"
        scope = f" matching '{file_path_pattern}'" if file_path_pattern else ""
        largest = ""
        if results:
            top = results[0]
            largest = (
                f"; largest: {display_name(top) or top['relative_path']} "
                f"({top['line_count']} lines)"
            )
        # One line, saying exactly what was applied. An agent asked for a top
        # 10 that got 6 rows back reported 6 as the top 10, because a 50-line
        # floor it never asked for was applied silently.
        if min_lines is None:
            if not results:
                summary = f"No {noun}{scope} in the graph"
            else:
                head = (f"All {matching}" if len(results) == matching
                        else f"Top {len(results)} of {matching}")
                summary = (f"{head} {noun}{scope} by line count "
                           f"(--limit {limit}, no --min-lines){largest}")
        elif results:
            shown = (f"; showing {len(results)} (--limit {limit})"
                     if len(results) < matching else "")
            summary = f"{matching} {noun} >= {min_lines} lines{scope}{shown}{largest}"
        else:
            summary = f"No {noun} >= {min_lines} lines{scope}"
        response: dict[str, Any] = {
            "status": "ok",
            "summary": summary,
            "total_found": len(results),
            "matching": matching,
            "results": results,
        }
        if min_lines is not None:
            response["min_lines"] = min_lines
        if excluded:
            summary += (
                f"; {excluded} in generated or declaration files not shown "
                "(--include-generated to include them)"
            )
            response["summary"] = summary
            response["excluded"] = {
                "generated_files": excluded,
                "include_with": "--include-generated",
            }
        return response
    finally:
        store.close()
