"""Opaque, versioned pagination cursors for the capability envelope.

The contract says ``page.next_cursor`` is "opaque, versioned, and bound to the
query hash plus the provenance snapshot". Each clause closes one failure:

- **Bound to provenance** is the clause that earns the module. Without it, a
  page 2 fetched after a rebuild returns rows from a different graph than page
  1, and nothing in the response says so. The agent receives a work queue that
  is a splice of two graphs and has no way to notice. Silent incorrectness is
  what is being designed out here; a rejected cursor is the loud, recoverable
  alternative.
- **Bound to the query** stops one command's offset being replayed against
  another's results, which yields a plausible-looking page of the wrong list.
- **Versioned** so a token minted by an older build is rejected rather than
  misread. The payload below is then free to change shape.
- **Opaque** so the agent neither parses nor constructs one, which is what
  keeps the payload free to change. Opacity is a contract promise, not a
  security boundary: this is base64url of compact JSON and anyone can read it.
  Nothing is signed. A forged cursor gains an attacker who already runs the
  CLI nothing they could not get by passing different arguments.

**Offset, not keyset.** Keyset paging would need every one of the ten pageable
commands to expose a stable, total sort key; none of them guarantees one, and
several rank by a score that is not unique. Offset is well defined precisely
because the cursor is bound to provenance: it names a position in a sequence
that cannot have changed underneath it, because a graph that changed produces a
different provenance digest and the cursor is refused. Offset paging is unsafe
against a mutating collection, and this collection is pinned.

Wiring into the CLI: ``docs/design/cursors.md``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from typing import Any, Mapping, Optional

from . import envelope as _envelope

#: Cursor payload version. Independent of the envelope's ``schema``: the
#: envelope can gain fields without invalidating every outstanding cursor, and
#: this payload can be reshaped without bumping the envelope every host pins.
VERSION = 1

#: Rendered as a fixed-width string rather than an integer so that the encoded
#: token keeps a constant length — see ``reserve``. Two digits covers the
#: foreseeable life of the format; widening it is itself a version bump.
_VERSION_FIELD = f"{VERSION:02d}"

#: Never a real version, so a placeholder that escapes to an agent is
#: diagnosed as the internal fault it is rather than reported as staleness.
_PLACEHOLDER_VERSION = "00"

#: Offsets are zero-padded to a fixed width for the same constant-length
#: reason. Ten digits is past any row count a code graph will hold.
_OFFSET_WIDTH = 10

#: 64 bits of digest. The width is about accident rates, not adversaries:
#: a collision would have to occur between two *different* queries against the
#: *same* graph, and its consequence is a wrong page rather than a breach.
_DIGEST_WIDTH = 16

#: Arguments that change how the answer is presented, not which rows it holds.
#: Stated as an exclusion list on purpose: a result-affecting flag added later
#: is bound automatically, and the failure mode of over-binding — a spurious
#: rejection the agent recovers from by re-running — is the one worth having.
#: Under-binding is the failure this module exists to prevent.
_PRESENTATION_ARGS = frozenset(
    {
        "output_format",  # --format: rendering only
        "max_tokens",     # --max-tokens: how much of the answer fits, not which
        "cursor",         # a cursor is not part of its own identity
    }
)


class CursorError(Exception):
    """A cursor was refused. Carries the remediation that clears it.

    One exception type for every rejection — stale, foreign, old-version,
    malformed — because the agent's recovery is identical in all four cases:
    drop the cursor and re-run. Splitting them would ask the agent to branch on
    a distinction it cannot act on. The *message* still distinguishes them, for
    whoever reads the log.
    """

    #: Actionable, and needs no human. Re-running without the cursor returns
    #: page 1 of the current graph, which is a correct answer to the question
    #: the agent was actually asking.
    remediation = "re-run the same command without --cursor to restart from the first page"


def _canonical(value: Any) -> str:
    """Stable JSON for hashing. ``default=str`` so a Path or enum still hashes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:_DIGEST_WIDTH]


def query_digest(
    command: str,
    args: Mapping[str, Any],
    *,
    extra: Optional[Mapping[str, Any]] = None,
) -> str:
    """Identify the query a cursor belongs to.

    ``args`` is the parsed argument namespace as a mapping (``vars(args)``);
    everything in it binds except ``_PRESENTATION_ARGS``.

    Hash the arguments the *caller* passed, never the ones derived from them.
    Offset paging fetches ``offset + limit`` rows and discards the head, so the
    fetch size differs page to page while ``--limit`` does not — digesting the
    derived value would make every cursor reject itself on redemption.

    ``extra`` carries values that decide the answer but do not appear in the
    namespace. The resolved repository root is the one that matters: ``--repo``
    is ``None`` whenever it was auto-detected, so two invocations from
    different directories are indistinguishable without it.
    """
    bound = {k: v for k, v in args.items() if k not in _PRESENTATION_ARGS}
    bound["command"] = command
    if extra:
        bound.update(extra)
    return _digest(bound)


def provenance_digest(provenance: Optional[Mapping[str, Any]]) -> str:
    """Identify the graph snapshot a cursor was issued against.

    Only the three contract fields participate, so an unrelated addition to the
    provenance block does not invalidate outstanding cursors.

    ``built_at`` moves on every incremental update, including one that changed
    nothing the query touches. That over-invalidates, deliberately: the cheap
    error is a page 1 the agent did not need to re-fetch, and the expensive one
    is a page 2 spliced from two graphs.

    A provenance block that is entirely null — no graph built, nothing to bind
    to — still digests to a stable value, so the cursor round-trips. That is
    not a weakened guarantee: the moment a build happens, ``graph_sha`` and
    ``built_at`` stop being null and the digest changes, which is exactly the
    event the binding exists to catch.
    """
    snapshot = {
        key: (provenance or {}).get(key)
        for key in ("graph_sha", "built_at", "schema_version")
    }
    return _digest(snapshot)


def _pack(version: str, offset: int, query: str, provenance: str) -> str:
    raw = _canonical(
        {
            "v": version,
            "o": f"{offset:0{_OFFSET_WIDTH}d}",
            "q": query,
            "p": provenance,
        }
    ).encode("utf-8")
    # Padding stripped: the input length is constant, so the output length is
    # too, and the trailing '=' is noise in a value that travels on a command
    # line. `_unpack` restores it.
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def encode(offset: int, *, query: str, provenance: str) -> str:
    """Mint a cursor for ``offset`` in the sequence ``query`` produced."""
    if offset < 0:
        raise ValueError("cursor offsets are positions in a sequence, never negative")
    return _pack(_VERSION_FIELD, offset, query, provenance)


#: A cursor-shaped token of exactly the width a real one occupies. See
#: ``reserve``.
PLACEHOLDER = _pack(_PLACEHOLDER_VERSION, 0, "0" * _DIGEST_WIDTH, "0" * _DIGEST_WIDTH)


def decode(token: str, *, query: str, provenance: str) -> int:
    """Read the offset out of a cursor, or refuse it.

    Raises ``CursorError`` and nothing else. Every failure here is reached by
    feeding the function a string that came from outside, so an escaping
    ``binascii.Error`` or ``JSONDecodeError`` would surface to the agent as an
    internal fault rather than as the recoverable condition it is.

    Checks run most-specific-first — version, then shape, then query, then
    provenance — so the message names the real cause. A v0 payload read under
    v1 field rules would fail some later check and be reported as staleness.
    """
    if not isinstance(token, str) or not token:
        raise CursorError("cursor is empty")
    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        # ValueError covers JSONDecodeError and base64's own length complaints.
        raise CursorError("cursor is not a cursor Cartograph issued") from None
    if not isinstance(payload, dict):
        raise CursorError("cursor is not a cursor Cartograph issued")

    version = payload.get("v")
    if version == _PLACEHOLDER_VERSION:
        # Reserved width that was never filled in. Not the agent's doing, and
        # worth naming: it means an emit path called `reserve` without ever
        # calling `finalise`.
        raise CursorError("cursor was never finalised — this is a Cartograph bug")
    if version != _VERSION_FIELD:
        raise CursorError("cursor was issued by a different version of Cartograph")

    offset = payload.get("o")
    if not isinstance(offset, str) or not offset.isdigit():
        raise CursorError("cursor is malformed")
    if payload.get("q") != query:
        raise CursorError("cursor belongs to a different query")
    if payload.get("p") != provenance:
        raise CursorError("the graph has been rebuilt since this cursor was issued")
    return int(offset)


def reserve(page: _envelope.Page) -> None:
    """Hold a cursor's worth of width in the page block before ``fit`` runs.

    ``fit`` decides how many rows survive a token budget by measuring the
    envelope, so anything added afterwards is unmeasured — the trap
    ``docs/design/token-budget.md`` records for ``truncated`` and for the
    synthesised ``page`` block. A cursor is not sheddable (without it page 2 is
    unreachable), so it cannot be added after the budget is settled.

    It also cannot be minted before: the offset it carries is the count of rows
    ``fit`` actually emitted, which is not known until ``fit`` has finished.

    The way out is that every cursor is exactly as long as every other one — a
    fixed-width version, a zero-padded offset and two fixed-width digests — so
    a placeholder can occupy the space during the measurement and ``finalise``
    can overwrite it with a token of identical length. The envelope never grows
    after fitting; it only ever shrinks, when there turns out to be no next
    page and the placeholder is replaced by ``null``.
    """
    page.next_cursor = PLACEHOLDER


def finalise(
    env: dict[str, Any],
    *,
    start: int,
    query: str,
    provenance: str,
) -> Optional[str]:
    """Replace the reserved placeholder with the cursor the response earned.

    ``start`` is the offset this page began at — 0 for a first page, otherwise
    the offset decoded from the incoming cursor.

    The next offset counts what was **emitted**, taken from
    ``page.result_count``, not what was fetched. ``fit`` may have dropped the
    tail of the collection to meet ``--max-tokens``; a cursor computed from the
    fetched count would step straight over the rows it dropped, and the agent
    would never learn they existed. This is the whole reason the cursor is
    minted here, after fitting, rather than where the page block is built.
    """
    page = env.get("page")
    if not isinstance(page, dict):
        return None

    emitted = page.get("result_count")
    token: Optional[str] = None
    if page.get("has_more") and isinstance(emitted, int):
        # An emitted count of zero mints a cursor that does not advance. That
        # is honest rather than useless: the position is preserved, and
        # `truncated_reason: max_tokens` is what tells the agent the budget —
        # not the cursor — is what has to change.
        token = encode(start + emitted, query=query, provenance=provenance)
    # `has_more` with no result_count is left alone: the envelope says more
    # exists and that it cannot be paged to, which is true. Guessing an offset
    # would be the one thing worse than a null cursor.
    page["next_cursor"] = token

    # Re-measure regardless. Overwriting the placeholder leaves the length
    # unchanged, but clearing it shrinks the envelope, and a `size` block that
    # overstates its payload is still a wrong size block.
    _envelope._with_size(env)
    budget = env.get("size", {}).get("budget_tokens")
    if budget:
        if env["size"]["tokens_estimated"] > budget:
            env["size"]["over_budget"] = True
        else:
            # Clearing the placeholder can bring an envelope back under budget.
            # Leaving the flag set would misreport a response that now fits.
            env["size"].pop("over_budget", None)
        _envelope._with_size(env)
    return token


def rejection(tool: str, exc: CursorError) -> dict[str, Any]:
    """Build the failure envelope for a refused cursor.

    **Exit 2, precondition.** The call was well formed, so ``usage`` (1) would
    misreport it: the agent did not get the arguments wrong, the graph moved.
    "Precondition" is read narrowly elsewhere as "no graph, run carto build",
    and a stale cursor is not something ``carto build`` fixes — but the code's
    meaning in the contract is a precondition of *this call* being unmet, and
    the cursor's precondition is that the graph is still the one it was minted
    against. That is exactly what failed.

    The decisive argument is structural. The schema requires ``remediation`` on
    precondition errors and nowhere else. Choosing ``precondition`` makes the
    self-heal instruction mandatory rather than optional, and recovering
    without a human is the requirement. ``internal`` (3) would be a lie about
    whose fault it is, and would tell the agent to give up.
    """
    return _envelope.error(
        tool,
        _envelope.Exit.PRECONDITION,
        str(exc),
        remediation=exc.remediation,
    )
