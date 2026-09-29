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


#: Upstream names its search strategies after their implementation ("fts" is
#: SQLite FTS5). Agents reason about whether *embeddings participated*, not
#: about which index answered, so the envelope normalises to the contract's
#: vocabulary here — the one seam every command passes through, so none of them
#: can leak an implementation name into an agent's context.
#:
#: Anything unrecognised degrades to "keyword" on purpose: over-claiming
#: "semantic" is the one error the contract exists to prevent.
_SEARCH_MODE = {
    "semantic": "semantic",
    "hybrid": "hybrid",     # embeddings AND lexical both contributed
    "fts": "keyword",       # FTS5 is lexical matching, whatever it is called
    "keyword": "keyword",
}


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
        env["search_mode"] = _SEARCH_MODE.get(search_mode, "keyword")
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


#: Size-block keys that describe the *budget* rather than the payload. They are
#: set by ``fit`` and must survive re-measurement.
_BUDGET_KEYS = ("budget_tokens", "over_budget")


def _with_size(env: dict[str, Any]) -> dict[str, Any]:
    """Attach the size block, measured on the serialised envelope itself.

    Measured to a fixed point: the size block is part of what gets serialised,
    so writing the count into it changes the count. Two passes are enough in
    practice (the second only differs by the digit-width of the first), but the
    loop is written to converge rather than to assume. It matters now that
    ``fit`` makes a hard promise about ``tokens_estimated``.
    """
    prior = env.get("size") or {}
    size: dict[str, Any] = {"chars": 0, "tokens_estimated": 0, "estimator": ESTIMATOR}
    for key in _BUDGET_KEYS:
        if key in prior:
            size[key] = prior[key]
    env["size"] = size
    for _ in range(4):
        chars = len(json.dumps(env, separators=(",", ":"), default=str))
        if chars == size["chars"]:
            break
        size["chars"] = chars
        size["tokens_estimated"] = estimate_tokens(chars)
    return env


#: How much each piece of supporting context is worth keeping when the budget
#: bites. Higher survives longer. Anything not named here ranks lowest and goes
#: first: context we cannot vouch for is the safest thing to lose, and that
#: default means a facet added later degrades gracefully instead of silently
#: outranking the diff.
#:
#: For `review-context` this produces exactly the documented order —
#: source_snippets (unranked, and much the largest) -> edges -> changed_nodes —
#: leaving the cheap, high-value file lists standing.
_CONTEXT_VALUE = {
    "changed_files": 3,   # the diff itself: smallest and most valuable
    "impacted_files": 2,
    "affected_files": 2,  # impact's file list with counts: its scope guarantee
    "changed_nodes": 1,
    "edges": 0,
}

#: Where the pageable collection lives when `page.collection` does not say.
_DEFAULT_COLLECTIONS = ("items", "results")

#: Keys in `data` that are never shed. `summary` is the floor — the part an
#: agent always needs, and a response without it is not a cheaper answer, it is
#: no answer. `facets` is the container, handled member by member.
_NEVER_SHED = frozenset({"summary", "facets"})


def _weigh(value: Any) -> int:
    return len(json.dumps(value, default=str))


def _shrink_facet(facets: dict[str, Any], name: str, omitted: list[str]) -> bool:
    """Reduce one facet as far as it goes. True if anything was given up.

    A facet shaped ``{items, total}`` keeps its shell: emptying ``items`` while
    keeping ``total`` costs a handful of characters and preserves the honesty
    signal, so the agent still learns that 48 edges exist. ``omitted`` records
    how many were withheld, because a bare ``"items": []`` next to a total is
    ambiguous — at a glance it reads like "none found".
    """
    facet = facets.get(name)
    if isinstance(facet, dict) and isinstance(facet.get("items"), list):
        if not facet["items"]:
            return False
        facet["omitted"] = len(facet["items"])
        facet["items"] = []
        return True
    if not facet:
        return False
    # A shape we do not understand (source_snippets is upstream's own blob), so
    # there is no shell worth keeping. Name it instead, so the loss is visible.
    del facets[name]
    omitted.append(name)
    return True


def _collection_key(env: dict[str, Any], data: Any) -> Optional[str]:
    """Name the one collection that is the *answer*, per contract amendment A8.

    Everything else list-shaped in ``data`` is supporting context by
    definition, which is what makes the drop order below principled rather
    than a list of special cases.
    """
    named = (env.get("page") or {}).get("collection")
    if named and isinstance(data, dict) and isinstance(data.get(named), list):
        return named
    if not isinstance(data, dict):
        return None
    conventional = next(
        (k for k in _DEFAULT_COLLECTIONS if isinstance(data.get(k), list)), None
    )
    if conventional:
        return conventional
    # A response with exactly one list has no ambiguity about which one is the
    # answer. This is what stops `capabilities` from shedding its whole command
    # catalogue: an agent shown zero commands concludes none exist, whereas a
    # trimmed-but-flagged list is merely incomplete.
    lists = [k for k, v in data.items() if isinstance(v, list) and k not in _NEVER_SHED]
    return lists[0] if len(lists) == 1 else None


def _sheddable(data: dict[str, Any], collection: Optional[str]) -> list[tuple[Any, ...]]:
    """Every unit of supporting context, in the order it should be given up.

    Least valuable first; among equals the largest, because that buys the most
    budget for the same loss of meaning. Two kinds live here: members of
    ``data.facets``, and top-level lists in ``data`` that are not the answer —
    upstream hangs supporting detail like `edges` directly off `data`, and
    shedding the answer while keeping its footnotes is exactly backwards.
    """
    targets: list[tuple[Any, ...]] = []
    facets = data.get("facets")
    if isinstance(facets, dict):
        targets += [
            (_CONTEXT_VALUE.get(n, -1), -_weigh(facets[n]), "facet", n)
            for n in facets
        ]
    targets += [
        (_CONTEXT_VALUE.get(k, -1), -_weigh(v), "top", k)
        for k, v in data.items()
        if isinstance(v, list) and v and k != collection and k not in _NEVER_SHED
    ]
    return sorted(targets, key=lambda t: t[:2])


def _largest_fitting_prefix(
    env: dict[str, Any], container: Any, key: Any, max_tokens: int
) -> int:
    """Binary-search the longest head of a list that keeps the envelope in budget.

    Head, not a sample: the collection is ranked, so the tail is the part the
    agent needs least. Binary search costs ~5 re-measurements on a 25-item
    page, far cheaper than emitting a response the caller has to discard.
    """
    full = list(container[key])
    low, high, best = 0, len(full), 0
    while low <= high:
        mid = (low + high) // 2
        container[key] = full[:mid]
        _with_size(env)
        if env["size"]["tokens_estimated"] <= max_tokens:
            best, low = mid, mid + 1
        else:
            high = mid - 1
    container[key] = full[:best]
    return best


def fit(env: dict[str, Any], max_tokens: Optional[int]) -> dict[str, Any]:
    """Reduce an envelope to a token budget *semantically*, never by byte cut.

    A byte cut hands the agent invalid JSON, which is strictly worse than a
    smaller valid answer: it cannot be parsed, so it cannot even be partially
    recovered. Nothing here truncates a string or a structure mid-way. Whole
    units of meaning are given up, cheapest loss first:

    1. **Supporting context** — facet members and the non-answer lists upstream
       hangs off ``data`` — least valuable first (see ``_CONTEXT_VALUE``).
    2. **The tail of the answer collection**, which is ranked, so its tail is
       the least relevant part of the work queue.
    3. Stop. ``data.summary`` is the floor and is never dropped.

    Anything given up sets ``truncated`` with reason ``max_tokens``, which
    outranks ``page_limit``: both may be true, but "narrow the query" is the
    advice that helps, and ``page.has_more`` still carries the rest.

    When the floor is still over budget the envelope says so via
    ``size.over_budget`` rather than quietly overrunning, which keeps the
    budget a promise Cartograph can always honour: either the response fits,
    or it admits on its face that it does not.
    """
    if not max_tokens or max_tokens <= 0:
        return env
    # An error envelope is already the floor: the remediation is the whole
    # point of emitting it, and a budget cannot override the one instruction
    # the agent needs in order to recover.
    if not env.get("ok", False):
        return env

    env.setdefault("size", {})["budget_tokens"] = max_tokens
    _with_size(env)
    if env["size"]["tokens_estimated"] <= max_tokens:
        return env

    # Set the truncation flags BEFORE reducing, not after. They cost characters
    # too, so deciding the budget without them measures an envelope that is not
    # the one being emitted — which is how a response lands over budget with
    # `over_budget` unset. They are withdrawn below if nothing is given up.
    was = (env.get("truncated"), env.get("truncated_reason"))
    env["truncated"] = True
    env["truncated_reason"] = "max_tokens"
    _with_size(env)

    gave_up = False
    data = env.get("data")
    collection = _collection_key(env, data)

    if isinstance(data, dict):
        omitted: list[str] = []
        facets = data.get("facets")
        for _, _, kind, name in _sheddable(data, collection):
            if env["size"]["tokens_estimated"] <= max_tokens:
                break
            if kind == "facet":
                shed = _shrink_facet(facets, name, omitted)
            else:
                del data[name]
                omitted.append(name)
                shed = True
            if shed:
                gave_up = True
                if omitted:
                    # Rewritten each pass so the note is measured along with
                    # everything else rather than appearing after the budget
                    # was already settled.
                    data["context_omitted"] = {
                        "keys": omitted,
                        "reason": "withheld to meet --max-tokens; re-run with a larger budget",
                    }
                _with_size(env)

    if env["size"]["tokens_estimated"] > max_tokens:
        container: Any = data if collection else None
        key: Any = collection
        if container is None and isinstance(data, list) and data:
            container, key = env, "data"
        if container is not None and container[key]:
            before = len(container[key])
            if not isinstance(env.get("page"), dict):
                # Installed BEFORE the search, not after: a response that was
                # whole is about to become a page of a larger list, and `page`
                # is what says so — but it also costs characters, and a block
                # added after the search would push the result back over the
                # budget the search just satisfied.
                env["page"] = Page(
                    limit=before,
                    has_more=True,
                    result_count=before,
                    total_estimated=before,
                    collection=None if key in _DEFAULT_COLLECTIONS else key,
                ).to_dict()
                _with_size(env)
            kept = _largest_fitting_prefix(env, container, key, max_tokens)
            if kept < before:
                gave_up = True
                # The page block must keep describing what was actually
                # emitted, or a cursor gets computed against a count that was
                # never sent. Narrowing these two only shrinks the envelope.
                env["page"]["result_count"] = kept
                env["page"]["has_more"] = True
                _with_size(env)

    if not gave_up:
        # Nothing was sheddable, so claiming truncation would be a lie.
        # Withdrawing the flags only shrinks the envelope, so this cannot
        # push a fitting response back over budget.
        env["truncated"], env["truncated_reason"] = was
        if env["truncated_reason"] is None:
            del env["truncated_reason"]
        if env["truncated"] is None:
            del env["truncated"]
        _with_size(env)

    # Adding this flag only grows the envelope, and it is only ever added when
    # the envelope is already over — so the verdict cannot invalidate itself.
    if env["size"]["tokens_estimated"] > max_tokens:
        env["size"]["over_budget"] = True
        _with_size(env)
    else:
        env["size"].pop("over_budget", None)
    return _reorder(env)


def _reorder(env: dict[str, Any]) -> dict[str, Any]:
    """Keep `truncated_reason` next to `truncated`.

    Ordering is semantically irrelevant to a JSON parser but not to a model
    reading the response, and `fit` would otherwise append the reason far from
    the flag it explains.
    """
    if "truncated_reason" not in env:
        return env
    reason = env.pop("truncated_reason")
    rebuilt: dict[str, Any] = {}
    for key, value in env.items():
        rebuilt[key] = value
        if key == "truncated":
            rebuilt["truncated_reason"] = reason
    rebuilt.setdefault("truncated_reason", reason)
    env.clear()
    env.update(rebuilt)
    return env


def emit(env: dict[str, Any], fmt: str = "json", max_tokens: Optional[int] = None) -> int:
    """Write the envelope to stdout and return the process exit code.

    In json mode nothing else may reach stdout.

    The budget is applied here rather than in each command, so every command
    inherits ``--max-tokens`` for free and none of them can forget it. It
    applies in text mode too: the budget is a property of the payload, not of
    how it happens to be rendered.
    """
    env = fit(env, max_tokens)
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
            if isinstance(value, (list, tuple)) and value:
                # A compact row is already a line; joining rows with commas
                # runs them together into one unreadable one.
                print(key.replace("_", " "))
                for item in value:
                    print(f"  {item}")
                continue
            if isinstance(value, (list, tuple)):
                value = "-"
            print(f"{key.replace('_', ' '):<{width}}  {value}")
    elif data is not None:
        print(data)
    if env.get("truncated"):
        print(
            f"\n(truncated: {env.get('truncated_reason')})",
            file=sys.stderr,
        )
