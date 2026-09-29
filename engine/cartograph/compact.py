"""Compact rows — the default shape of a list an agent reads.

A graph node serialised whole is twelve fields, and for most rows four of them
carry the same path: ``name``, ``qualified_name``, ``file_path`` and
``relative_path`` of a File node are one string written four times. Add
``id``, ``parent_name: null``, ``is_test: false`` and ``language`` and a row
costs ~350 characters, where what an agent acts on — what it is, where it is,
and the one number the command was asked about — fits in ~60. Measured on
``large-functions`` over a 991-file repository: 20 rows, 7,172 characters
whole, 1,254 as rows like these.

So list commands answer with one string per row by default::

    301 lines | Function | Store.migrate | src/store.ts:20
    CALLS | runMatrix -> src/store.ts::Store.migrate | scripts/run.ts:512

A node's qualified name, which ``carto query`` accepts as a target, is
``<path>::<name>`` from the row. ``--detail full`` returns the rows whole.

This module is the only place a row is shortened. Each command names which of
its lists hold which kind of row; nothing else about a command lives here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

DETAIL_CHOICES = ("compact", "full")
DEFAULT_DETAIL = "compact"

#: A confidence tier is only worth characters when it is not the default.
_DEFAULT_TIER = "EXTRACTED"

#: Upstream's `_hints` name MCP tools (`query_graph`, `get_flow`) that no
#: longer exist here. Choosing the next step is the skill's job; the same call
#: was made for review-context's `next_tool_suggestions`. A tool's own
#: `truncated` is restated by the envelope's `truncated` and `page` blocks,
#: which the emit path fills from it before compacting.
_ALWAYS_DROP = ("_hints", "truncated")


def _location(row: dict[str, Any]) -> tuple[Optional[str], Optional[int]]:
    """The path and first line of a row, whichever shape it arrived in.

    Graph nodes say ``file_path``/``line_start``, dead-code items ``file``/
    ``line``, and review-context items nest both under ``location``.
    """
    loc = row.get("location") if isinstance(row.get("location"), dict) else {}
    path = row.get("file_path") or row.get("file") or loc.get("file")
    line = row.get("line_start") or row.get("line") or loc.get("line_start")
    return path, line


def _qualified(row: dict[str, Any]) -> Optional[str]:
    # review-context items carry the qualified name as `id`; graph nodes carry
    # a numeric `id`, which is not a name.
    ident = row.get("id")
    return row.get("qualified_name") or (ident if isinstance(ident, str) else None)


def display_name(row: dict[str, Any]) -> Optional[str]:
    """The part of a node's identity its path does not already say.

    ``src/store.ts::Store.migrate`` in ``src/store.ts`` is ``Store.migrate`` —
    which keeps the parent a method belongs to, and joins back to the qualified
    name as ``<path>::<name>``. A File node's identity is its path, so it has
    no name of its own. Anything shaped otherwise is shown whole.
    """
    path, _ = _location(row)
    qualified = _qualified(row)
    if qualified and path:
        if qualified == path:
            return None
        if qualified.startswith(f"{path}::"):
            return qualified[len(path) + 2:]
    return qualified or row.get("name") or row.get("title")


def node_row(
    node: dict[str, Any], metric: Optional[tuple[str, str]] = None,
) -> Any:
    """``[<n> <unit> | ]<kind> | [<name> | ]<path>[:<line>]``.

    ``metric`` names the field a command is about and its unit, e.g.
    ``("line_count", "lines")``. Line 1 of a File is not a location worth
    printing.
    """
    kind = node.get("kind")
    path, line = _location(node)
    if not kind or not path:
        # Not a node this knows how to shorten. Passing it through whole is
        # the honest failure; guessing would drop something that mattered.
        return node
    parts: list[str] = []
    if metric and node.get(metric[0]) is not None:
        parts.append(f"{node[metric[0]]} {metric[1]}")
    parts.append(kind)
    name = display_name(node)
    if name:
        parts.append(name)
    parts.append(f"{path}:{line}" if line and kind != "File" else path)
    return " | ".join(parts)


def impact_row(node: dict[str, Any]) -> Any:
    """``<direct|transitive> | <node row>``.

    Direct means one hop from the change. It leads because it is the ranking's
    first key and the thing an agent weighs first: a direct dependent breaks
    when a signature changes; a transitive one may not.
    """
    row = node_row(node)
    if not isinstance(row, str) or "direct" not in node:
        return row
    return f"{'direct' if node['direct'] else 'transitive'} | {row}"


def file_count_row(entry: dict[str, Any]) -> Any:
    """``<path> | <n> items[ (<d> direct)]``."""
    if "file" not in entry or "items" not in entry:
        return entry
    row = f"{entry['file']} | {entry['items']} item{'s' if entry['items'] != 1 else ''}"
    return f"{row} ({entry['direct']} direct)" if entry.get("direct") else row


def handle_row(node: dict[str, Any]) -> Any:
    """``<kind> | <qualified name> | line <n>`` — for rows that exist to be passed back.

    A disambiguation list is only useful as targets for the next call, and
    ``query``'s own hint says to pass a qualified name from it, so here the
    name is kept whole rather than split around its path.
    """
    kind, qualified = node.get("kind"), _qualified(node)
    _, line = _location(node)
    if not kind or not qualified:
        return node
    return " | ".join([kind, qualified] + ([f"line {line}"] if line else []))


def _endpoint(name: Optional[str], path: Optional[str]) -> str:
    # The source of an edge nearly always lives in the file the edge was found
    # in, so its path is already on the row.
    if name and path and name.startswith(f"{path}::"):
        return name[len(path) + 2:]
    return name or "?"


def edge_row(edge: dict[str, Any]) -> Any:
    """``<KIND> | <source> -> <target> | <path>:<line>[ | <tier>]``."""
    kind, path = edge.get("kind"), edge.get("file_path") or edge.get("file")
    if not kind or "source" not in edge or "target" not in edge:
        return edge
    parts = [
        kind,
        f"{_endpoint(edge.get('source'), path)} -> {_endpoint(edge.get('target'), path)}",
    ]
    if path:
        parts.append(f"{path}:{edge['line']}" if edge.get("line") else path)
    tier = edge.get("confidence_tier") or (
        edge.get("confidence") if isinstance(edge.get("confidence"), str) else None
    )
    if tier and tier != _DEFAULT_TIER:
        parts.append(tier)
    for resolution in ("ambiguous", "unresolved"):
        count = edge.get(f"{resolution}_target_count")
        if count:
            parts.append(f"{count} {resolution} targets")
    return " | ".join(parts)


def flow_row(flow: dict[str, Any]) -> Any:
    """``id <n> | <name> | criticality <c> | <n> nodes, <n> files, depth <d>``.

    The id leads because ``carto flow --id`` is how an agent reads one; the
    node-id path is left to ``--detail full``, since no command takes it.
    """
    if "id" not in flow or "name" not in flow:
        return flow
    return (
        f"id {flow['id']} | {flow['name']} | criticality {flow.get('criticality')} | "
        f"{flow.get('node_count')} nodes, {flow.get('file_count')} files, "
        f"depth {flow.get('depth')}"
    )


def community_row(community: dict[str, Any]) -> Any:
    """``id <n> | <name> | <size> nodes | cohesion <c>``.

    A member sample is a handful of names out of thousands; ``carto community
    --members`` is the way to read them.
    """
    if "id" not in community or "name" not in community:
        return community
    return (
        f"id {community['id']} | {community['name']} | "
        f"{community.get('size')} nodes | cohesion {community.get('cohesion')}"
    )


def _with_metric(field_name: str, unit: str) -> Callable[[dict[str, Any]], Any]:
    return lambda node: node_row(node, metric=(field_name, unit))


@dataclass(frozen=True)
class _Spec:
    #: Where each row list lives in ``data``, and how its rows are written.
    rows: dict[tuple[str, ...], Callable[[dict[str, Any]], Any]]
    #: Top-level keys that repeat something else in the response.
    drop: tuple[str, ...] = field(default_factory=tuple)


_SPECS: dict[str, _Spec] = {
    "large-functions": _Spec(
        rows={("results",): _with_metric("line_count", "lines")},
        # Echoes of the caller's own argument and of page.result_count.
        drop=("min_lines", "total_found"),
    ),
    "query": _Spec(
        rows={
            ("results",): node_row,
            ("edges",): edge_row,
            ("disambiguation",): handle_row,
        },
        # `candidates` is the same list as `disambiguation`, kept upstream for
        # an older key name; `description` restates the pattern name.
        drop=("candidates", "description"),
    ),
    # `search_mode` is carried by the envelope, normalised; the copy in `data`
    # is the store's implementation name ("fts"), which is the one spelling
    # the envelope exists to keep away from an agent.
    "search": _Spec(rows={("results",): node_row}, drop=("query", "search_mode")),
    # Edges are the bulk of an impact response (2,507 of them, 84% of it, on
    # one file of a 991-file repository) and `totals.edges` counts them;
    # changed_nodes are the contents of the files the caller named, counted in
    # `totals.changed_nodes`. `impacted_files` is `affected_files` without the
    # counts, and the rest restate `totals`. `--detail full` keeps all of it.
    "impact": _Spec(
        rows={
            ("impacted_nodes",): impact_row,
            ("affected_files",): file_count_row,
        },
        drop=("changed_nodes", "edges", "impacted_files", "total_impacted",
              "nodes_omitted"),
    ),
    "dead-code": _Spec(rows={("items",): node_row}),
    "flows": _Spec(rows={("flows",): flow_row}),
    "communities": _Spec(rows={("communities",): community_row}),
    "review-context": _Spec(
        rows={
            ("items",): node_row,
            ("facets", "changed_nodes", "items"): node_row,
            ("facets", "edges", "items"): edge_row,
        },
    ),
}

#: The commands that take ``--detail``. The parser attaches the flag from this,
#: so a command cannot advertise a flag that does nothing for it.
COMMANDS = frozenset(_SPECS)


def _container(data: dict[str, Any], path: tuple[str, ...]) -> Optional[dict[str, Any]]:
    node: Any = data
    for key in path[:-1]:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else None


def compact(command: str, result: Any) -> Any:
    """Rewrite a command's result into compact rows, in place. Idempotent.

    Rows already written as strings are left alone, so a result that passes
    through twice is not shortened twice.
    """
    spec = _SPECS.get(command)
    if spec is None or not isinstance(result, dict):
        return result
    for path, render in spec.rows.items():
        holder = _container(result, path)
        rows = holder.get(path[-1]) if holder is not None else None
        if isinstance(rows, list):
            holder[path[-1]] = [render(r) if isinstance(r, dict) else r for r in rows]
    for key in (*_ALWAYS_DROP, *spec.drop):
        result.pop(key, None)
    for key in [k for k, v in result.items() if v is None]:
        del result[key]
    if result.get("status") == "ok":
        # The envelope's own `ok` already says it.
        del result["status"]
    return result
