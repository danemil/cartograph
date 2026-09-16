"""The Cartograph capability envelope (contract v1).

Every ``carto`` command speaks this shape under ``--format json``. It is the
seam between Cartograph and any host: there is no per-host adapter on the query
path, so this module is the whole interface.

Schema: ``contracts/capability-v1/schemas/envelope.schema.json``.

Two rules that are easy to break and expensive to debug:

1. **stdout carries nothing but the envelope** in json mode. Logs go to stderr.
   The same discipline the MCP stdio server needed, for the same reason.
2. **This is the query protocol, not the hook protocol.** Hooks are invoked by
   the host, emit a host-defined shape exactly once, and there exit code ``2``
   means *blocking feedback to the model* — not "precondition failed". Never
   route a hook through these helpers.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Optional

SCHEMA_VERSION = 1

#: The documented estimator. Conservative, monotonic with output size, and
#: model-agnostic — a budget signal, never a claim about a specific tokenizer.
ESTIMATOR = "chars/4"


class Exit:
    """Exit codes for the query protocol."""

    OK = 0
    #: Bad arguments. The agent got the call wrong.
    USAGE = 1
    #: A precondition is unmet (no graph, no database). ALWAYS carries an
    #: actionable ``error.remediation`` so the agent can self-heal instead of
    #: failing the user's task.
    PRECONDITION = 2
    #: Something broke inside Cartograph.
    INTERNAL = 3


_CODE_FOR_EXIT = {
    Exit.USAGE: "usage",
    Exit.PRECONDITION: "precondition",
    Exit.INTERNAL: "internal",
}


@dataclass
class Page:
    """Paging for the one collection an envelope may page.

    At most one collection per envelope is pageable — a cursor could not be
    interpreted unambiguously otherwise. ``collection`` names it when the result
    is composite rather than a plain item list.
    """

    limit: int
    has_more: bool = False
    next_cursor: Optional[str] = None
    result_count: Optional[int] = None
    total_estimated: Optional[int] = None
    collection: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"limit": self.limit, "has_more": self.has_more}
        if self.collection is not None:
            out["collection"] = self.collection
        out["next_cursor"] = self.next_cursor
        if self.result_count is not None:
            out["result_count"] = self.result_count
        if self.total_estimated is not None:
            out["total_estimated"] = self.total_estimated
        return out


def estimate_tokens(chars: int) -> int:
    """Conservative token estimate from a character count."""
    return -(-chars // 4)  # ceil division


def ok(
    tool: str,
    data: Any = None,
    *,
    provenance: Optional[dict[str, Any]] = None,
    page: Optional[Page] = None,
    truncated: bool = False,
    truncated_reason: Optional[str] = None,
    search_mode: Optional[str] = None,
) -> dict[str, Any]:
    """Build a success envelope.

    Empty results are a SUCCESS, not an error — an agent asking "who calls this
    function?" about something nothing calls got a correct answer.
    """
    env: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "tool": tool,
        "data": data,
        "truncated": truncated,
    }
    if truncated:
        # The schema requires this whenever truncated is true: it tells the
        # agent whether to narrow the query or ask for the next page.
        env["truncated_reason"] = truncated_reason or "max_tokens"
    if search_mode is not None:
        # Required on search-like results. Degradation to keyword/FTS5 must
        # never be silent — an agent must not mistake it for semantic search.
        env["search_mode"] = search_mode
    if provenance is not None:
        env["provenance"] = provenance
    if page is not None:
        env["page"] = page.to_dict()
    return _with_size(env)


def error(
    tool: str,
    exit_code: int,
    message: str,
    *,
    remediation: Optional[str] = None,
) -> dict[str, Any]:
    """Build a failure envelope.

    The envelope is emitted on stdout even on failure, so an agent parsing json
    never has to fall back to scraping stderr.
    """
    code = _CODE_FOR_EXIT.get(exit_code, "internal")
    if code == "precondition" and not remediation:
        raise ValueError(
            "precondition errors must carry a remediation — it is what lets "
            "the agent recover instead of failing the user's task"
        )
    env: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "tool": tool,
        "data": None,
        "error": {"code": code, "message": message},
    }
    if remediation:
        env["error"]["remediation"] = remediation
    return _with_size(env)


def _with_size(env: dict[str, Any]) -> dict[str, Any]:
    """Attach the size block, measured on the serialised envelope itself."""
    env["size"] = {"chars": 0, "tokens_estimated": 0, "estimator": ESTIMATOR}
    chars = len(json.dumps(env, separators=(",", ":"), default=str))
    env["size"] = {
        "chars": chars,
        "tokens_estimated": estimate_tokens(chars),
        "estimator": ESTIMATOR,
    }
    return env


def emit(env: dict[str, Any], fmt: str = "json") -> int:
    """Write the envelope to stdout and return the process exit code.

    In json mode nothing else may reach stdout.
    """
    if fmt == "json":
        print(json.dumps(env, indent=2, default=str))
    else:
        _print_text(env)
    if env.get("ok"):
        return Exit.OK
    code = env.get("error", {}).get("code")
    return {"usage": Exit.USAGE, "precondition": Exit.PRECONDITION}.get(
        code, Exit.INTERNAL
    )


def _print_text(env: dict[str, Any]) -> None:
    """Human-readable rendering. Never parsed by anything."""
    if not env.get("ok"):
        err = env.get("error", {})
        print(f"error: {err.get('message', 'unknown error')}", file=sys.stderr)
        if err.get("remediation"):
            print(f"  try: {err['remediation']}", file=sys.stderr)
        return
    data = env.get("data")
    if isinstance(data, dict):
        width = max((len(k) for k in data), default=0)
        for key, value in data.items():
            if isinstance(value, (list, tuple)):
                value = ", ".join(str(v) for v in value) or "-"
            print(f"{key.replace('_', ' '):<{width}}  {value}")
    elif data is not None:
        print(data)
    if env.get("truncated"):
        print(
            f"\n(truncated: {env.get('truncated_reason')})",
            file=sys.stderr,
        )
