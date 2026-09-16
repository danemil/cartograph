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


def run_operation(manifest: dict, op_name: str, op: dict, tmpdir: str, res: Result) -> None:
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

    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run(["git", "init", "-q", tmpdir], check=False)
        for op_name, op in manifest["operations"].items():
            run_operation(manifest, op_name, op, tmpdir, res)

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
