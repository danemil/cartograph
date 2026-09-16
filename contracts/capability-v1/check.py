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
      "command": [".venv/bin/python", "-m", "code_review_graph"],
      "cwd": "engine",
      "operations": {
        "status": {"args": ["status"], "expect": "ok"},
        "status-no-graph": {"args": ["status", "--repo", "{tmpdir}"],
                            "expect": "precondition"}
      }
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


def run_operation(manifest: dict, op_name: str, op: dict, tmpdir: str, res: Result) -> None:
    base = list(manifest["command"])
    args = [a.replace("{tmpdir}", tmpdir) for a in op["args"]]
    cwd = manifest.get("cwd")
    expect = op.get("expect", "ok")

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
