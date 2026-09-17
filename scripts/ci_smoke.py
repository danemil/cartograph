#!/usr/bin/env python3
"""Assert a freshly frozen engine parses, not merely that it starts.

A payload whose tree-sitter grammars did not come along parses nothing and
still exits 0 — `build` reports success over an empty graph. Cartograph has
shipped that once. An exit-code check would not have caught it, so this asserts
on a node count.

Kept in Python rather than inline shell because it runs identically on the
Windows runner, where the shell does not.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def main(target: str) -> int:
    root = Path("dist/payload") / target
    manifest = json.loads((root / "PAYLOAD.json").read_text())
    carto = root / manifest["executable"]

    subprocess.run([str(carto), "--version"], check=True)

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "a.py").write_text("def f():\n    return 1\n")
        (repo / "b.ts").write_text("export function g(): number {\n  return 2;\n}\n")
        subprocess.run([str(carto), "build", "--repo", str(repo), "--quiet"], check=True)
        out = subprocess.run(
            [str(carto), "status", "--repo", str(repo), "--format", "json"],
            check=True, capture_output=True, text=True,
        )

    data = json.loads(out.stdout)["data"]
    nodes = data.get("nodes") or 0
    if nodes <= 0:
        print(f"FAIL: the frozen engine built an EMPTY graph: {data}", file=sys.stderr)
        return 1
    print(f"ok: {nodes} nodes, {data.get('files')} files, languages={data.get('languages')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
