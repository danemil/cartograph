#!/usr/bin/env python3
"""Cartograph capability contract — conformance check.

Runs a tool under test through the contract and asserts the *protocol*, never
the implementation. It shells out; it must never import product code, because
the same suite has to hold a Python engine and a TypeScript memory side to the
same standard.

    python contracts/capability-v1/check.py --manifest engine/contract-manifest.json

A manifest maps abstract operations to whatever concrete command that tool uses:

    {
      "name": "engine",
      "command": [".venv/bin/python", "-m", "cartograph"],
      "cwd": "engine",
      "operations": {
        "status": {"args": ["status"], "expect": "ok"},
        "status-no-graph": {"args": ["status", "--repo", "{tmpdir}"],
                            "expect": "precondition"}
      }
    }

An operation may also carry a token budget, which appends --max-tokens and
turns on the budget checks:

    "review-context-budget": {
      "args": ["review-context", "--repo", "."],
      "expect": "ok",
      "budget": 1200,          # assert it fits, or declares over_budget
      "expect_truncated": true, # assert the budget is what forced truncation
      "expect_floor": "summary" # assert this data key survived the reduction
    }

An operation may also pin the paging block, which is how a zero-result case
proves it does not advertise a page that is not there:

    "search-zero-results": {
      "args": ["search", "no_such_symbol", "--repo", ".", "--limit", "7"],
      "expect": "ok",
      "expect_page": {"limit": 7, "has_more": false, "result_count": 0}
    }

``"expect_page": false`` asserts the opposite — that no page block is emitted,
which is the honest answer for a command with no result cap.

An operation may also assert that nothing in ``data`` carries the checkout's
own absolute path. Ids are what an agent hands back to the next call, so they
have to be worth carrying; the prefix is machine-specific noise charged on
every row of every response:

    "search-repo-relative": {
      "args": ["search", "cli", "--repo", ".", "--limit", "5"],
      "expect": "ok",
      "expect_repo_relative": true
    }
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA = json.loads((HERE / "schemas" / "envelope.schema.json").read_text())

EXIT_FOR = {"ok": 0, "usage": 1, "precondition": 2, "internal": 3}


class Result:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        if condition:
            self.passed.append(name)
        else:
            self.failed.append((name, detail))


def validate_schema(doc: dict) -> list[str]:
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return ["jsonschema not installed — cannot validate (pip install jsonschema)"]
    return [
        f"{list(e.path)}: {e.message}"
        for e in Draft202012Validator(SCHEMA).iter_errors(doc)
    ]



def _checkout_root(manifest_path: Path) -> str:
    """The checkout whose absolute prefix must never reach an agent.

    Found from the manifest rather than from the current directory, so the
    assertion cannot quietly go vacuous when the suite is run from elsewhere.
    """
    here = manifest_path.resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    return str(Path.cwd().resolve())


def _absolute_leaks(value, root: str) -> list[str]:
    """Every string under `data` that still begins with the checkout's path.

    Anchored at the start, because quoted file content legitimately mentions
    absolute paths mid-line and rewriting a source line would be a lie.
    """
    found: list[str] = []

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if isinstance(key, str) and key.startswith(root):
                    found.append(key)
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str) and node.startswith(root):
            found.append(node)

    walk(value)
    return found


def _collection_of(doc: dict) -> list:
    """The one pageable collection in a response, whatever it is named."""
    data = doc.get("data") or {}
    named = (doc.get("page") or {}).get("collection")
    for key in ([named] if named else []) + ["items", "results"]:
        if key and isinstance(data.get(key), list):
            return data[key]
    return []


def _check_second_page(manifest, op_name, op, base, args, first, cwd, res) -> None:
    """Fetch page two with the cursor page one issued, and prove it continues."""
    tag = f"{op_name}"
    cursor = (first.get("page") or {}).get("next_cursor")
    res.check(f"{tag}: page one issues a cursor", bool(cursor),
              "has_more was true but next_cursor was null")
    if not cursor:
        return

    proc = subprocess.run(
        base + args + ["--cursor", cursor],
        cwd=cwd, capture_output=True, text=True, timeout=60,
    )
    try:
        second = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        res.check(f"{tag}: page two is a single JSON envelope", False, str(exc))
        return
    res.check(f"{tag}: page two is a single JSON envelope", True)
    res.check(f"{tag}: page two succeeds", second.get("ok") is True,
              str(second.get("error")))

    rows_one = [json.dumps(r, sort_keys=True) for r in _collection_of(first)]
    rows_two = [json.dumps(r, sort_keys=True) for r in _collection_of(second)]
    overlap = set(rows_one) & set(rows_two)
    res.check(f"{tag}: page two does not repeat page one", not overlap,
              f"{len(overlap)} row(s) returned twice")

    # The real test of an offset: one call for the whole span must produce the
    # two pages concatenated. Anything else means the fetch-and-discard slipped.
    limit = (first.get("page") or {}).get("limit")
    if limit and rows_two:
        widened = [a if a != str(limit) else str(limit * 2) for a in args]
        proc = subprocess.run(
            base + widened, cwd=cwd, capture_output=True, text=True, timeout=60,
        )
        try:
            whole = [json.dumps(r, sort_keys=True)
                     for r in _collection_of(json.loads(proc.stdout))]
        except json.JSONDecodeError:
            return
        res.check(
            f"{tag}: the two pages are the single-call sequence",
            whole[: len(rows_one) + len(rows_two)] == rows_one + rows_two,
            "page one + page two diverges from one call covering both",
        )


def run_operation(
    manifest: dict, op_name: str, op: dict, tmpdir: str, res: Result,
    checkout: str = "",
) -> None:
    base = list(manifest["command"])
    args = [a.replace("{tmpdir}", tmpdir) for a in op["args"]]
    cwd = manifest.get("cwd")
    expect = op.get("expect", "ok")
    budget = op.get("budget")
    if budget is not None:
        args += ["--max-tokens", str(budget)]

    # Every agent-facing invocation is json; text mode is for humans only.
    proc = subprocess.run(
        base + args + ["--format", "json"],
        cwd=cwd, capture_output=True, text=True, timeout=60,
    )
    tag = f"{op_name}"

    # 1. stdout must be the envelope and nothing else.
    try:
        doc = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        res.check(f"{tag}: stdout is a single JSON envelope", False,
                  f"{exc}; got {proc.stdout[:120]!r}")
        return
    res.check(f"{tag}: stdout is a single JSON envelope", True)

    # 2. It must satisfy the schema.
    errs = validate_schema(doc)
    res.check(f"{tag}: envelope matches schema", not errs, "; ".join(errs[:3]))

    # 3. Exit code must agree with the envelope.
    want = EXIT_FOR[expect]
    res.check(f"{tag}: exit {want} ({expect})", proc.returncode == want,
              f"got {proc.returncode}")

    # 4. ok must agree with the expectation.
    res.check(f"{tag}: ok == {expect == 'ok'}", doc.get("ok") is (expect == "ok"))

    # 5. Preconditions must be actionable, or the agent cannot self-heal.
    if expect == "precondition":
        rem = doc.get("error", {}).get("remediation")
        res.check(f"{tag}: precondition carries a remediation", bool(rem),
                  "missing error.remediation")

    # 5b. Paging must describe the page the CALLER asked for. A `limit` echoed
    #     back as the number of rows that happened to come back makes
    #     `has_more` the tautology len >= len, so an empty result advertises a
    #     next page that does not exist — and the agent pages forever.
    want_page = op.get("expect_page")
    if want_page is not None:
        page = doc.get("page")
        if want_page is False:
            res.check(f"{tag}: no page block", page is None, f"got {page}")
        else:
            res.check(f"{tag}: carries a page block", isinstance(page, dict))
            for field, value in (want_page or {}).items():
                res.check(f"{tag}: page.{field} == {value!r}",
                          isinstance(page, dict) and page.get(field) == value,
                          f"got {page.get(field)!r}" if isinstance(page, dict) else "no page")

    # 5c. Ids are handed straight back to the next call, so an absolute path in
    #     `data` is both machine-specific and charged on every row. The
    #     resolver re-anchors a repo-relative target against the repo root, so
    #     nothing is lost by shortening it.
    if op.get("expect_repo_relative") and checkout:
        leaks = _absolute_leaks(doc.get("data"), checkout)
        res.check(
            f"{tag}: data carries no absolute checkout paths",
            not leaks,
            f"{len(leaks)} value(s), e.g. {leaks[0]!r}" if leaks else "",
        )

    # 6. The size block must actually describe the payload.
    size = doc.get("size", {})
    res.check(f"{tag}: size.chars is plausible",
              isinstance(size.get("chars"), int) and size["chars"] > 0)
    res.check(f"{tag}: size.estimator is documented", size.get("estimator") == "chars/4")

    # 7. Truncation must say why — the agent needs to know whether to narrow
    #    the query or ask for the next page.
    if doc.get("truncated"):
        res.check(f"{tag}: truncated implies truncated_reason",
                  bool(doc.get("truncated_reason")))

    # A cursor must actually continue. The unit tests pin this against a
    # synthetic collection; only the CLI can pin it against a real one, through
    # the same fetch-and-discard path an agent would use. Above the budget gate
    # below, because paging has nothing to do with whether a budget was set.
    if op.get("page_twice") and doc.get("ok"):
        _check_second_page(manifest, op_name, op, base, args, doc, cwd, res)

    if budget is None:
        return

    # 8. The budget must be visible, so an agent can tell that it was applied
    #    rather than assume it.
    res.check(f"{tag}: size.budget_tokens echoes --max-tokens",
              size.get("budget_tokens") == budget,
              f"got {size.get('budget_tokens')}")

    # 9. THE PROMISE: the response fits, or it says on its face that it does
    #    not. A response that quietly overruns is the failure mode a token
    #    budget exists to prevent.
    tokens = size.get("tokens_estimated")
    res.check(f"{tag}: fits the budget, or declares over_budget",
              tokens <= budget or size.get("over_budget") is True,
              f"tokens_estimated={tokens} > budget={budget} with over_budget unset")

    # 10. Truncation is SEMANTIC. Parsing already proved the document is whole
    #     (a byte cut would not parse); this proves the *cause* is reported as
    #     the budget, so the agent narrows instead of paging.
    if op.get("expect_truncated"):
        res.check(f"{tag}: budget truncation is flagged", doc.get("truncated") is True)
        res.check(f"{tag}: truncated_reason is max_tokens",
                  doc.get("truncated_reason") == "max_tokens",
                  f"got {doc.get('truncated_reason')!r}")

    # 11. The floor holds: whatever else is shed, the part an agent always
    #     needs must survive. A response without it is not a cheaper answer.
    floor = op.get("expect_floor")
    if floor:
        data = doc.get("data") or {}
        res.check(f"{tag}: {floor} survives the budget",
                  bool(data.get(floor)),
                  f"{floor} missing or empty under --max-tokens {budget}")

    # 12. Paging must keep describing what was actually emitted, or a cursor
    #     gets computed against a count that was never sent.
    page = doc.get("page")
    if isinstance(page, dict) and page.get("result_count") is not None:
        data = doc.get("data") or {}
        name = page.get("collection") or next(
            (k for k in ("items", "results") if isinstance(data.get(k), list)), None
        )
        if name and isinstance(data.get(name), list):
            res.check(f"{tag}: page.result_count matches what was emitted",
                      page["result_count"] == len(data[name]),
                      f"page says {page['result_count']}, data.{name} has {len(data[name])}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", required=True, type=Path)
    args = ap.parse_args()

    manifest = json.loads(args.manifest.read_text())
    res = Result()
    checkout = _checkout_root(args.manifest)

    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run(["git", "init", "-q", tmpdir], check=False)
        for op_name, op in manifest["operations"].items():
            run_operation(manifest, op_name, op, tmpdir, res, checkout)

    name = manifest.get("name", args.manifest.stem)
    for label in res.passed:
        print(f"  ok    {label}")
    for label, detail in res.failed:
        print(f"  FAIL  {label}" + (f"\n          {detail}" if detail else ""))

    total = len(res.passed) + len(res.failed)
    print(f"\n{name}: {len(res.passed)}/{total} checks passed")
    return 1 if res.failed else 0


if __name__ == "__main__":
    sys.exit(main())
