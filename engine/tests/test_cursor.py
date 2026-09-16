"""Pagination cursors: what they bind to, and what they refuse.

The condition this module guards is not a crash. It is a page 2 that continues
against a graph rebuilt since page 1 and says nothing about it — an answer
spliced from two graphs, indistinguishable from a correct one. Most of the
cases below are therefore about *refusal*, and the rest are about the offset
being computed from what was emitted rather than from what was fetched, which
is the other way rows go missing without a trace.

Wiring and rationale: ``docs/design/cursors.md``, ``cartograph/cursor.py``.
"""

from __future__ import annotations

import json

import pytest

from cartograph import cursor, envelope

_PROVENANCE = {
    "graph_sha": "6d3ed84c0ffee1234567890abcdef0123456789a",
    "built_at": "2026-09-16T10:00:00Z",
    "schema_version": 1,
}


def _args(**overrides):
    """A plausible `carto query` namespace, as ``vars(args)`` would render it."""
    base = {
        "command": "query",
        "pattern": "callers",
        "target": "engine/cartograph/envelope.py::fit",
        "repo": None,
        "detail_level": "standard",
        "max_results": 25,
        "output_format": "json",
        "max_tokens": None,
        "cursor": None,
    }
    base.update(overrides)
    return base


def _digests(args=None, provenance=None):
    args = _args() if args is None else args
    return (
        cursor.query_digest(args["command"], args),
        cursor.provenance_digest(_PROVENANCE if provenance is None else provenance),
    )


def _rows(count, first=0):
    return [
        {
            "id": f"engine/cartograph/module_{n:03d}.py::handler_{n:03d}",
            "title": f"handler_{n:03d}",
            "kind": "Function",
            "location": {"file": f"engine/cartograph/module_{n:03d}.py", "line": n},
        }
        for n in range(first, first + count)
    ]


def _page_envelope(rows, *, limit, has_more, reserve=True):
    """One page of `carto query` results, shaped the way the CLI shapes them."""
    page = envelope.Page(
        limit=limit,
        has_more=has_more,
        result_count=len(rows),
    )
    if reserve:
        cursor.reserve(page)
    return envelope.ok(
        "query",
        data={"results": rows, "summary": {"total_matches": 200, "risk": "medium"}},
        page=page,
        provenance=_PROVENANCE,
    )


# --------------------------------------------------------------------------
# Encoding
# --------------------------------------------------------------------------


def test_a_cursor_round_trips_to_the_offset_it_was_minted_for():
    query, provenance = _digests()
    token = cursor.encode(25, query=query, provenance=provenance)
    assert cursor.decode(token, query=query, provenance=provenance) == 25


def test_a_cursor_does_not_advertise_its_contents():
    """Opaque is a contract promise, so the token must not read as structure.

    Not a secrecy claim — it is base64 and trivially decoded. The point is that
    an agent looking at it finds no offset to increment by hand.
    """
    query, provenance = _digests()
    token = cursor.encode(25, query=query, provenance=provenance)
    assert "25" not in token
    assert query not in token


@pytest.mark.parametrize("offset", [0, 1, 25, 9_999, 999_999_999])
def test_every_cursor_occupies_the_same_width(offset):
    """The invariant `reserve` rests on: a placeholder can stand in for any token."""
    query, provenance = _digests()
    assert len(cursor.encode(offset, query=query, provenance=provenance)) == len(
        cursor.PLACEHOLDER
    )


def test_a_negative_offset_is_a_programming_error_not_a_refusal():
    """No agent input reaches this, so it raises rather than minting nonsense."""
    query, provenance = _digests()
    with pytest.raises(ValueError):
        cursor.encode(-1, query=query, provenance=provenance)


# --------------------------------------------------------------------------
# What the digests bind
# --------------------------------------------------------------------------


def test_presentation_arguments_do_not_change_the_query():
    """`--format` and `--max-tokens` change how much of the answer fits, not which
    rows it holds. Binding them would reject a cursor for no reason."""
    plain = cursor.query_digest("query", _args())
    dressed = cursor.query_digest(
        "query", _args(output_format="text", max_tokens=800, cursor="whatever")
    )
    assert plain == dressed


@pytest.mark.parametrize(
    "change",
    [
        {"pattern": "callees"},
        {"target": "engine/cartograph/cli.py::main"},
        {"detail_level": "full"},
        {"max_results": 50},
        {"command": "impact"},
    ],
)
def test_result_shaping_arguments_do_change_the_query(change):
    assert cursor.query_digest("query", _args()) != cursor.query_digest(
        change.get("command", "query"), _args(**change)
    )


def test_an_argument_nobody_thought_about_still_binds():
    """The exclusion list is the design: a flag added later binds by default.

    Over-binding costs a spurious rejection the agent recovers from. The
    opposite mistake is the one this module exists to prevent.
    """
    assert cursor.query_digest("query", _args()) != cursor.query_digest(
        "query", _args(some_future_filter="tests-only")
    )


def test_an_auto_detected_repo_is_told_apart_by_the_resolved_root():
    """`--repo` is None whenever it was auto-detected, so two invocations from
    different directories are identical without the resolved root."""
    args = _args()
    assert cursor.query_digest(
        "query", args, extra={"repo_root": "/work/cartograph"}
    ) != cursor.query_digest("query", args, extra={"repo_root": "/work/other"})


@pytest.mark.parametrize(
    "change",
    [
        {"graph_sha": "0000000000000000000000000000000000000000"},
        {"built_at": "2026-09-16T10:00:01Z"},
        {"schema_version": 2},
    ],
)
def test_any_move_in_the_provenance_snapshot_changes_the_digest(change):
    assert cursor.provenance_digest(_PROVENANCE) != cursor.provenance_digest(
        {**_PROVENANCE, **change}
    )


def test_an_unrelated_provenance_field_does_not_invalidate_cursors():
    """Only the three contract fields participate, so the block can grow."""
    assert cursor.provenance_digest(_PROVENANCE) == cursor.provenance_digest(
        {**_PROVENANCE, "host": "copilot-cli"}
    )


def test_an_unbuilt_graph_still_round_trips_but_a_build_invalidates_it():
    empty = cursor.provenance_digest({"graph_sha": None, "built_at": None})
    assert empty == cursor.provenance_digest(None)
    assert empty != cursor.provenance_digest(_PROVENANCE)


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def test_the_same_offset_against_a_different_query_is_refused():
    query, provenance = _digests()
    token = cursor.encode(25, query=query, provenance=provenance)
    other = cursor.query_digest("query", _args(pattern="callees"))
    with pytest.raises(cursor.CursorError, match="different query"):
        cursor.decode(token, query=other, provenance=provenance)


def test_a_cursor_issued_before_a_rebuild_is_refused():
    """The failure the module exists for: page 2 must not silently continue
    against a graph that was rebuilt between the two calls."""
    query, provenance = _digests()
    token = cursor.encode(25, query=query, provenance=provenance)
    rebuilt = cursor.provenance_digest({**_PROVENANCE, "graph_sha": "deadbeef" * 5})
    with pytest.raises(cursor.CursorError, match="rebuilt"):
        cursor.decode(token, query=query, provenance=rebuilt)


def test_a_cursor_from_another_version_is_refused_rather_than_misread():
    """Minted the way a future build would mint it — same fields, later version.

    Read under v1 rules the payload would decode to a perfectly plausible
    offset, which is why the version is checked before anything else.
    """
    query, provenance = _digests()
    token = cursor._pack("02", 25, query, provenance)
    with pytest.raises(cursor.CursorError, match="different version"):
        cursor.decode(token, query=query, provenance=provenance)


@pytest.mark.parametrize(
    "garbage",
    [
        "",
        "!!!not base64!!!",
        "Zm9vYmFy",  # valid base64, not JSON
        "MTIz",  # valid base64, JSON, but an integer rather than an object
        "eyJvIjoiMSJ9",  # an object without a version
        "bnVsbA",  # JSON null
        cursor.encode(25, query="q" * 16, provenance="p" * 16)[:-20],  # truncated
        cursor.encode(25, query="q" * 16, provenance="p" * 16) + "AAAA",  # extended
    ],
)
def test_garbage_is_refused_and_nothing_else_escapes(garbage):
    """Every one of these arrives from outside the process. A binascii or JSON
    error reaching the agent would report a recoverable condition as a crash."""
    query, provenance = _digests()
    with pytest.raises(cursor.CursorError):
        cursor.decode(garbage, query=query, provenance=provenance)


def test_a_non_string_cursor_is_refused():
    query, provenance = _digests()
    with pytest.raises(cursor.CursorError):
        cursor.decode(None, query=query, provenance=provenance)


def test_an_unfinalised_placeholder_names_itself_as_a_bug():
    """A reserved width that reached an agent means an emit path called
    `reserve` and never called `finalise`. Reporting it as staleness would
    send whoever debugs it looking at the graph instead of the code."""
    query, provenance = _digests()
    with pytest.raises(cursor.CursorError, match="never finalised"):
        cursor.decode(cursor.PLACEHOLDER, query=query, provenance=provenance)


def test_a_refused_cursor_is_a_precondition_the_agent_can_clear_alone():
    """Exit 2 with a mandatory remediation. The call was well formed, so
    `usage` would misreport it, and `internal` would tell the agent to stop."""
    with pytest.raises(cursor.CursorError) as refusal:
        cursor.decode("garbage", query="q" * 16, provenance="p" * 16)

    env = cursor.rejection("query", refusal.value)
    assert env["ok"] is False
    assert env["error"]["code"] == "precondition"
    assert "--cursor" in env["error"]["remediation"]
    assert envelope.emit(env, "json") == envelope.Exit.PRECONDITION


# --------------------------------------------------------------------------
# The interaction with fit()
# --------------------------------------------------------------------------


def test_the_cursor_counts_what_was_emitted_not_what_was_fetched():
    """`fit` drops the tail of the collection to meet a budget. An offset taken
    from the fetched count would step over the dropped rows, and the agent
    would have no way to learn they ever existed."""
    query, provenance = _digests()
    fetched = _rows(40)
    env = _page_envelope(fetched, limit=40, has_more=True)
    envelope.fit(env, 400)

    emitted = env["data"]["results"]
    assert 0 < len(emitted) < len(fetched), "the budget must actually bite here"

    token = cursor.finalise(env, start=0, query=query, provenance=provenance)
    assert cursor.decode(token, query=query, provenance=provenance) == len(emitted)
    assert env["page"]["result_count"] == len(emitted)


def test_paging_through_a_trimmed_collection_skips_nothing():
    """The end-to-end consequence: page 2 begins at the first row page 1 did
    not carry, not at the first row page 1 did not fetch."""
    query, provenance = _digests()
    corpus = _rows(120)

    first = _page_envelope(corpus[:40], limit=40, has_more=True)
    envelope.fit(first, 400)
    token = cursor.finalise(first, start=0, query=query, provenance=provenance)

    offset = cursor.decode(token, query=query, provenance=provenance)
    shown = first["data"]["results"]
    assert shown[-1] == corpus[offset - 1]

    second = _page_envelope(corpus[offset : offset + 40], limit=40, has_more=True)
    assert second["data"]["results"][0] == corpus[offset]


def test_a_reserved_cursor_leaves_the_response_inside_its_budget():
    """The reason `reserve` exists: the cursor is measured by `fit` along with
    everything else, so stamping it afterwards cannot overrun the budget."""
    query, provenance = _digests()
    env = _page_envelope(_rows(40), limit=40, has_more=True)
    envelope.fit(env, 400)
    cursor.finalise(env, start=0, query=query, provenance=provenance)

    assert env["size"]["tokens_estimated"] <= 400
    assert "over_budget" not in env["size"]
    assert env["size"]["chars"] == len(
        json.dumps(env, separators=(",", ":"), default=str)
    )


def test_an_unreserved_page_block_admits_the_overrun_rather_than_hiding_it():
    """`fit` synthesises a `page` block when a budget forces a trim on a result
    that was never paged, and nothing reserved width in it. Stamping a cursor
    there grows a settled envelope, so the response says `over_budget` instead
    of quietly exceeding what the caller asked for."""
    query, provenance = _digests()
    env = _page_envelope(_rows(40), limit=40, has_more=True, reserve=False)
    envelope.fit(env, 400)
    assert "over_budget" not in env["size"]

    cursor.finalise(env, start=0, query=query, provenance=provenance)
    assert env["size"]["tokens_estimated"] > 400
    assert env["size"]["over_budget"] is True


def test_no_next_page_means_no_cursor():
    """Withdrawing the reserved width only shrinks the envelope, so a last page
    can never be pushed over budget by the cursor it does not carry."""
    query, provenance = _digests()
    env = _page_envelope(_rows(5), limit=25, has_more=False)
    reserved = env["size"]["chars"]

    assert cursor.finalise(env, start=0, query=query, provenance=provenance) is None
    assert env["page"]["next_cursor"] is None
    assert env["size"]["chars"] < reserved


def test_a_continued_page_counts_from_where_it_started():
    query, provenance = _digests()
    env = _page_envelope(_rows(25, first=25), limit=25, has_more=True)

    token = cursor.finalise(env, start=25, query=query, provenance=provenance)
    assert cursor.decode(token, query=query, provenance=provenance) == 50


def test_a_page_that_fits_nothing_holds_its_position():
    """A budget too small for even one row yields a cursor that does not
    advance. Preserving the position is honest; `truncated_reason` is what
    tells the agent the budget, not the cursor, is what has to change."""
    query, provenance = _digests()
    env = _page_envelope(_rows(40), limit=40, has_more=True)
    envelope.fit(env, 60)
    assert env["data"]["results"] == []

    token = cursor.finalise(env, start=10, query=query, provenance=provenance)
    assert cursor.decode(token, query=query, provenance=provenance) == 10
    assert env["truncated_reason"] == "max_tokens"


def test_an_envelope_without_a_page_block_gets_no_cursor():
    query, provenance = _digests()
    env = envelope.ok("architecture", data={"layers": ["cli", "engine"]})
    assert cursor.finalise(env, start=0, query=query, provenance=provenance) is None
    assert "page" not in env


def test_has_more_without_a_count_yields_no_cursor_rather_than_a_guess():
    """The envelope then says more exists and cannot be paged to, which is
    true. Inventing an offset would be the one outcome worse than a null."""
    query, provenance = _digests()
    env = envelope.ok(
        "query",
        data={"results": _rows(3)},
        page=envelope.Page(limit=25, has_more=True),
    )
    assert cursor.finalise(env, start=0, query=query, provenance=provenance) is None
    assert env["page"]["next_cursor"] is None
    assert env["page"]["has_more"] is True
