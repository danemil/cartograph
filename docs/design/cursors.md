---
tags: [design, contract, cursors, paging, engine]
created: 2026-09-16
status: module and tests landed; CLI wiring pending
---

# Pagination cursors

`engine/cartograph/cursor.py`, tested by `engine/tests/test_cursor.py`.
The module is complete and self-contained. **Nothing in `cli.py` calls it yet**
— the wiring is at the bottom of this document, to be applied by hand.

## The promise

> **A page 2 either continues the same query against the same graph, or it is
> refused.**

`page.next_cursor` is, per the contract, "opaque, versioned, and bound to the
query hash plus the provenance snapshot". The binding is the point. Without it,
a page 2 fetched after a rebuild returns rows from a different graph than page
1, and no field in the response says so — the agent gets a work queue spliced
from two graphs and cannot tell. That is the failure being designed out, and a
loud refusal the agent clears on its own is the alternative.

| Clause | What it closes |
|---|---|
| Bound to provenance | Page 2 continuing against a rebuilt graph |
| Bound to the query | One command's offset replayed against another's results |
| Versioned | A token from an older build read under today's field rules |
| Opaque | An agent constructing or incrementing an offset by hand |

Opacity is a contract promise, not a security boundary. The token is base64url
of compact JSON, nothing is signed, and a forged cursor buys an attacker who
already runs the CLI nothing they could not get by passing different arguments.

## The API

```python
query_digest(command: str, args: Mapping[str, Any], *, extra=None) -> str
provenance_digest(provenance: Mapping[str, Any] | None) -> str

encode(offset: int, *, query: str, provenance: str) -> str
decode(token: str, *, query: str, provenance: str) -> int   # raises CursorError

reserve(page: envelope.Page) -> None
finalise(env: dict, *, start: int, query: str, provenance: str) -> str | None

rejection(tool: str, exc: CursorError) -> dict     # the exit-2 envelope
PLACEHOLDER: str
VERSION: int = 1
```

`decode` raises `CursorError` and nothing else. Every string it sees came from
outside the process, so an escaping `binascii.Error` or `JSONDecodeError` would
surface a recoverable condition to the agent as an internal fault.

### What binds into the query digest

An **exclusion** list, not an inclusion list: `--format`, `--max-tokens` and
`--cursor` are excluded, and everything else in the argument namespace binds.
A result-affecting flag added later is therefore bound by default. Over-binding
costs a spurious rejection the agent clears by re-running; under-binding is the
silent incorrectness the module exists to prevent, so the default falls on the
recoverable side.

`extra` carries what decides the answer but is not in the namespace. The
resolved repository root is the one that matters: `--repo` is `None` whenever
it was auto-detected, so two invocations from different directories are
otherwise indistinguishable.

Hash the arguments the **caller** passed, never the ones derived from them.
Offset paging widens the fetch to `offset + limit`, which differs page to page
while `--limit` does not; digesting the derived value would make every cursor
reject itself on redemption.

### What binds into the provenance digest

`graph_sha`, `built_at`, `schema_version` — the three contract fields, so an
unrelated addition to the provenance block does not invalidate live cursors.

`built_at` moves on every incremental update, including one that touched
nothing the query reads. That over-invalidates deliberately: the cheap error is
a page 1 the agent did not need to re-fetch.

## Rejection: exit 2, `precondition`

A refused cursor is a `precondition` error (exit 2) with the remediation
`re-run the same command without --cursor to restart from the first page`.

`usage` (1) is "the agent got the call wrong", and it did not — the call was
well formed and the graph moved underneath it. `internal` (3) would be a lie
about whose fault it is and would tell the agent to give up.

"Precondition" is read narrowly elsewhere as *no graph, run `carto build`*, and
`carto build` does not fix a stale cursor. But the code's meaning in the
contract is that a precondition of *this call* is unmet, and the cursor's
precondition is that the graph is still the one it was minted against. That is
precisely what failed.

The decisive argument is structural: the schema requires `remediation` on
precondition errors and nowhere else. Choosing `precondition` makes the
self-heal instruction mandatory rather than optional, and recovering without a
human is the requirement. One exception type covers stale, foreign, old-version
and malformed, because the recovery is identical in all four; the message still
distinguishes them for whoever reads the log.

## The interaction with `fit()`

This is the subtle part.

`fit()` trims the tail of the answer collection to meet `--max-tokens`, so the
rows emitted can be fewer than the rows fetched. **The cursor counts what was
emitted.** An offset taken from the fetched count steps straight over the rows
`fit` dropped, and the agent never learns they existed — the same silent loss
the provenance binding exists to prevent, arriving by a different route.

So the cursor has to be minted *after* fitting. But
`docs/design/token-budget.md` records why nothing may be added after fitting:
flags cost characters, and a block installed after the budget is settled
measures an envelope that is not the one being emitted. A cursor is not
sheddable either — without it page 2 is unreachable.

The way out is that **every cursor is exactly the same length**: a fixed-width
version field, a zero-padded offset, two fixed-width digests. So:

1. `reserve(page)` puts `PLACEHOLDER` in `page.next_cursor` *before* `fit`
   runs, and `fit` measures and budgets with it present.
2. `finalise(env, …)` overwrites it with a token of identical length, reading
   the offset from `page.result_count` — which `fit` has already narrowed to
   what it actually emitted.

The envelope never grows after fitting. It only shrinks, when there is no next
page and the placeholder is replaced by `null`.

Where nothing reserved the width — `fit` synthesises a `page` block of its own
when a budget forces a trim on a result that was never paged — `finalise`
re-measures and sets `size.over_budget`. The response then admits the overrun
instead of quietly exceeding what the caller asked for, which is the same
promise `--max-tokens` already makes. In the wiring below this case does not
arise: only commands in `_PAGEABLE_COLLECTION` get a cursor, and they always
build a `Page`.

A page that fits *nothing* (`result_count` 0, `has_more` true) mints a cursor
that does not advance. Preserving the position is honest; `truncated_reason:
max_tokens` is what tells the agent the budget, not the cursor, is what has to
change.

## Offset, not keyset

Keyset paging would need all eight pageable commands to expose a stable total
sort key. None guarantees one and several rank by a non-unique score.

Offset is well defined *because* the cursor is bound to provenance: it names a
position in a sequence that cannot have changed underneath it, since a graph
that changed produces a different provenance digest and the cursor is refused.
Offset paging is unsafe against a mutating collection, and this collection is
pinned.

**Known limitation, not fixed here.** The tools have no offset of their own, so
page 2 is served by fetching `offset + limit` rows and discarding the head.
Where a tool's row limit only truncates a ranked list, that is exact. Where it
caps work *before* ranking, a wider fetch can explore more of the graph, so the
first `offset` rows of the wider fetch need not match the rows page 1 showed,
and deep paging can repeat or miss a row. Closing that needs an offset
parameter on the tools themselves. `impact` was recorded here as such a case;
it is not: both traversal engines explore the whole radius and apply `--limit`
to the finished ranking, which is a total order (direct first, score, then
qualified name), so its pages are exact.

---

# Wiring

Four edits, all in `engine/cartograph/cli.py`. Line numbers are omitted on
purpose — the file is under concurrent edit; anchor on the quoted code.

### 1. `--cursor`, on the pageable commands only

`carto capabilities` is generated from this parser, so a command that cannot
honour a cursor must not advertise one. Add a second loop after the existing
`for _graph_cmd in (…)` block that attaches `--format` and `--max-tokens`:

```python
    # Only the eight commands in _PAGEABLE_COLLECTION have a collection a
    # cursor could name. `capabilities` is generated from this parser, so an
    # unpageable command advertising --cursor would make the catalogue lie.
    for _paged_cmd in (
        rc_cmd, query_cmd, impact_cmd, search_cmd,
        flows_cmd, communities_cmd, large_cmd, refactor_cmd,
    ):
        _paged_cmd.add_argument(
            "--cursor",
            default=None,
            help="Continue from a previous page (opaque; use page.next_cursor verbatim)",
        )
```

### 2. Read provenance at the dispatch site

In `main()`, inside `if args.command in _GRAPH_TOOL_COMMANDS:`, immediately
after the `if not db_path.exists():` precondition guard and before
`_run_graph_tool_command(args, repo_root)`:

```python
        from .graph import GraphStore

        # Every graph-tool response gains the provenance block the contract
        # already specified. Cursors bind to it, and an agent cannot otherwise
        # tell that two pages came from two builds.
        with GraphStore(db_path) as _store:
            provenance = {
                "graph_sha": _store.get_metadata("git_head_sha"),
                "built_at": _store.get_metadata("last_updated"),
            }
        _run_graph_tool_command(args, repo_root, provenance)
```

### 3. Decode, widen the fetch, thread it through

Add beside `_PAGEABLE_COLLECTION`:

```python
#: Where each pageable command keeps its row limit. Offset paging fetches
#: through the requested window, so this is the value that grows by the offset
#: — and pointedly NOT a value that binds into the cursor: it is derived from
#: the caller's arguments, not passed by the caller.
_LIMIT_DEST = {
    "review-context": "max_results",
    "query": "max_results",
    "impact": "max_results",
    "search": "limit",
    "flows": "limit",
    "large-functions": "limit",
    # communities and refactor take no row limit; they page by slicing alone.
}
```

Change the signature of `_run_graph_tool_command` to
`(args, repo_root: Path, provenance: dict | None = None)` and put this at the
top of its body, before any tool is called:

```python
    from . import cursor as _cursor
    from . import envelope as _env

    query = _cursor.query_digest(
        args.command, vars(args), extra={"repo_root": str(repo_root)}
    )
    snapshot = _cursor.provenance_digest(provenance)

    offset = 0
    if getattr(args, "cursor", None):
        try:
            offset = _cursor.decode(args.cursor, query=query, provenance=snapshot)
        except _cursor.CursorError as exc:
            env = _cursor.rejection(args.command, exc)
            raise SystemExit(_env.emit(env, getattr(args, "output_format", "json")))

    dest = _LIMIT_DEST.get(args.command)
    page_limit = getattr(args, dest, None) if dest else None
    if offset and page_limit:
        # Widen the fetch so the requested page is inside it. AFTER the digest
        # is taken: the cursor binds what the caller passed, and a cursor
        # minted at --limit 25 would reject itself on redemption if the widened
        # value were hashed instead.
        setattr(args, dest, page_limit + offset)
```

and change the last line of the function from `_emit_tool_result(args, result)`
to:

```python
    _emit_tool_result(
        args, result,
        offset=offset, page_limit=page_limit,
        query=query, snapshot=snapshot, provenance=provenance,
    )
```

### 4. `_emit_tool_result`: slice, reserve, fit, finalise

New signature:

```python
def _emit_tool_result(
    args, result: dict, *,
    offset: int = 0, page_limit: int | None = None,
    query: str | None = None, snapshot: str | None = None,
    provenance: dict | None = None,
) -> None:
```

Replace the page-building block and the final emit with:

```python
    from . import cursor as _cursor

    page = None
    key = _PAGEABLE_COLLECTION.get(command)
    if key and isinstance(result, dict) and isinstance(result.get(key), list):
        if offset:
            # The tools have no offset of their own, so the earlier pages' rows
            # were fetched again. This is the one place that knows which
            # collection pages, so it is where they are discarded — and it runs
            # before result_count is taken, or the count would describe rows
            # the caller already has.
            result[key] = result[key][offset:]
        items = result[key]
        # `minimum: 1` in the schema, so an empty collection still needs a
        # limit that is a number of rows rather than zero of them.
        limit = max(page_limit or len(items), 1)
        page = _env.Page(
            limit=limit,
            # With no row limit the tool returned everything it had, so nothing
            # follows this page. `len(items) >= limit` reads as has_more on a
            # complete list and would mint a cursor that pages past the end —
            # which is what `communities` and `refactor` do today.
            has_more=bool(page_limit) and len(items) >= page_limit,
            result_count=len(items),
            collection=None if key in ("items", "results") else key,
        )
        if query is not None:
            # Hold the width now: fit() budgets with the cursor present, so
            # stamping it afterwards cannot overrun. See docs/design/cursors.md.
            _cursor.reserve(page)

    truncated = bool(result.get("truncated")) if isinstance(result, dict) else False

    env = _env.ok(
        command,
        data=result,
        provenance=provenance,
        page=page,
        truncated=truncated,
        truncated_reason="page_limit" if truncated else None,
        search_mode=result.get("search_mode") if isinstance(result, dict) else None,
    )
    env = _env.fit(env, getattr(args, "max_tokens", None))
    if query is not None and page is not None:
        _cursor.finalise(env, start=offset, query=query, provenance=snapshot)
    # The budget is already applied. Passing it to emit() would fit a second
    # time and could trim below the count the cursor was just minted from.
    raise SystemExit(_env.emit(env, fmt, None))
```

Note the `limit` change: the current code reads `getattr(args, "max_results")`
and falls back to `len(items)`, which is wrong for `search`, `flows` and
`large-functions`, whose dest is `limit`. `has_more` is inferred from it, so
paging those three needs `page_limit` to be the real value.

### Then

- `./scripts/verify.sh` — the envelope conformance suite reads `page` and will
  hold the new `next_cursor` to the schema.
- Add a conformance case that pages a real command twice and asserts the second
  page's first row is the first row the first page did not carry. The unit
  tests pin this against a synthetic collection; only the CLI can pin it
  against a real one.
- Update `skills/` if any skill body should mention `--cursor`, then re-run
  `scripts/install-skills.py`. `check_skills.py` validates every `carto` line
  in a skill against `carto capabilities`, so a flag mentioned before it exists
  fails the suite.
