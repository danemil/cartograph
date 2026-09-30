#!/usr/bin/env python3
"""Assert a freshly frozen engine works offline, not merely that it starts.

Two claims, each of which has failed silently before or could:

* **It parses.** A payload whose tree-sitter grammars did not come along
  parses nothing and still exits 0 — `build` reports success over an empty
  graph. Cartograph has shipped that once. So this asserts on a node count.
* **Memory search is meaning-based, with no network.** The payload carries an
  embedding model and the runtime to run it. A reworded question must find a
  memory keyword search cannot, while the operating system refuses the binary
  any network access: `sandbox-exec` on macOS, a network namespace on Linux,
  an outbound firewall rule on Windows. If the block cannot be put in place
  the run fails rather than proving nothing.

Then it prints what the memory path costs on this runner — `mem search` with
and without the model, peak RSS, embedding time per row, payload size — so the
numbers exist for every platform, not only the machine the design was written
on.

Everything runs as the launcher would run it: the grammar and model variables
point into the payload, and nothing else is on the environment's side.

Kept in Python rather than inline shell because it runs identically on the
Windows runner, where the shell does not.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

#: Memories as the project records them, and a question sharing no searchable
#: word with the first. The same fixture as engine/tests/test_mem_embeddings.py,
#: which proves the unfrozen engine; this proves the frozen one.
MEMORIES = [
    ("Rejected FalkorDB for the graph store",
     "Its SSPLv1 licence is not something the client's legal team will sign off, "
     "and it needs a daemon running. Staying on SQLite."),
    ("Offset paging for mem search",
     "Cursors bind to the provenance snapshot; agents re-issue a query rather "
     "than holding state between calls."),
    ("Hooks always exit 0",
     "Copilot reads exit code 2 as blocking feedback to the model, so a hook "
     "that fails must stay silent."),
]
REWORDED = "which storage option did we turn down over the licensing terms"
EXPECTED = "Rejected FalkorDB for the graph store"

#: Rows embedded in one `mem sync` to time the per-row cost, each about as
#: long as a session summary (~150 words, under the model's 256-token window)
#: so the figure is per realistic memory, not per short line.
TIMED_ROWS = 100
_TIMED_BODY = (
    "Worked on the payload build for the Linux target. The grammar cache was "
    "seeded from the build machine and copied into the payload, then the "
    "launcher was changed to point the engine at it through an environment "
    "variable, the same way the extension does. Decided to keep the pointer "
    "file rather than baking an absolute path into the launcher, because an "
    "extension update moves the directory and a baked path would fail later "
    "and elsewhere. Proposed, not yet confirmed: pruning grammars nobody "
    "selects, which would save about forty megabytes. Dead end: building the "
    "Linux payload on this Mac, since PyInstaller freezes the interpreter it "
    "runs on and cannot cross-compile. Next: run the offline acceptance in a "
    "container with no network and compare node counts with the unfrozen "
    "engine on the same repository, then record the numbers. Row {n}."
)

#: Anything that tries the network by name gets a closed port instead.
_DEAD = "http://127.0.0.1:9"


def payload_env(root: Path, *, model: bool) -> dict[str, str]:
    env = {
        **os.environ,
        "TREE_SITTER_LANGUAGE_PACK_CACHE_DIR": str(root / "grammars"),
        "TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL": _DEAD,
        "HF_HUB_OFFLINE": "1",
        "HF_ENDPOINT": _DEAD,
        "HTTP_PROXY": _DEAD,
        "HTTPS_PROXY": _DEAD,
    }
    env.pop("CARTO_EMBEDDING_MODEL_DIR", None)
    if model:
        env["CARTO_EMBEDDING_MODEL_DIR"] = str(root / "model")
    return env


class Offline:
    """An OS-level network block around the frozen binary, or a failure."""

    RULE = "cartograph-ci-offline"

    def __init__(self, carto: Path) -> None:
        self.carto = carto
        self.prefix: list[str] = []

    def __enter__(self) -> "Offline":
        if sys.platform == "darwin":
            self.prefix = ["sandbox-exec", "-p", "(version 1)(allow default)(deny network*)"]
            self._probe()
        elif sys.platform.startswith("linux"):
            # Needs CAP_SYS_ADMIN; the CI container is granted it for this.
            self.prefix = ["unshare", "--net"]
            self._probe()
        elif sys.platform == "win32":
            # Scoped to this executable, so the runner keeps its own network.
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"New-NetFirewallRule -DisplayName {self.RULE} -Direction Outbound "
                 f"-Program '{self.carto}' -Action Block | Out-Null"],
                check=True,
            )
            print(f"offline: outbound firewall rule {self.RULE} blocks {self.carto}")
        else:
            raise SystemExit(f"FAIL: no network block for {sys.platform}")
        return self

    def _probe(self) -> None:
        """The block must actually block: a connection attempt under it fails."""
        probe = subprocess.run(
            [*self.prefix, sys.executable, "-c",
             "import socket; socket.create_connection(('1.1.1.1', 443), timeout=5)"],
            capture_output=True, text=True,
        )
        if probe.returncode == 0:
            raise SystemExit(f"FAIL: {' '.join(self.prefix)} did not block the network")
        if "Traceback" not in probe.stderr:
            # The wrapper itself failed (no permission for a namespace, say),
            # so python never ran: that is not a block, it is no test at all.
            raise SystemExit(f"FAIL: could not set up the network block: {probe.stderr.strip()}")
        print(f"offline: {self.prefix[0]} refuses connections")

    def __exit__(self, *_exc: object) -> None:
        if sys.platform == "win32":
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Remove-NetFirewallRule -DisplayName {self.RULE}"],
                check=False,
            )


def carto_json(argv: list[str], env: dict[str, str]) -> dict:
    out = subprocess.run(argv, env=env, capture_output=True, text=True)
    try:
        return json.loads(out.stdout)
    except ValueError:
        raise SystemExit(
            f"FAIL: {' '.join(argv)} exited {out.returncode} without an envelope\n"
            f"stdout: {out.stdout[:600]}\nstderr: {out.stderr[-1500:]}"
        )


def timed(argv: list[str], env: dict[str, str]) -> tuple[float, Optional[int]]:
    """Wall seconds and peak RSS in bytes (None where it cannot be read)."""
    start = time.perf_counter()
    proc = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if sys.platform == "win32":
        peak = _windows_peak(proc)
        proc.wait()
        return time.perf_counter() - start, peak
    _, _, usage = os.wait4(proc.pid, 0)
    proc.returncode = 0  # reaped above; stops Popen reaping it again
    # ru_maxrss is bytes on macOS and kilobytes on Linux. Under a wrapper
    # (sandbox-exec, unshare) it is the largest process in the tree, which is
    # the engine.
    peak = usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024)
    return time.perf_counter() - start, peak


def _windows_peak(proc: subprocess.Popen) -> Optional[int]:
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in (
                "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")
        ]

    handle = int(proc._handle)  # noqa: SLF001 — the only way to the process handle
    # Waited for first: a finished process's counters stay readable through
    # the handle Popen still holds.
    ctypes.windll.kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
    counters = Counters()
    counters.cb = ctypes.sizeof(Counters)
    ok = ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
    return int(counters.PeakWorkingSetSize) if ok else None


def check_graph(carto: Path, root: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        (repo / "a.py").write_text("def f():\n    return 1\n")
        (repo / "b.ts").write_text("export function g(): number {\n  return 2;\n}\n")
        env = payload_env(root, model=True)
        subprocess.run([str(carto), "build", "--repo", str(repo), "--quiet"], check=True, env=env)
        data = carto_json(
            [str(carto), "status", "--repo", str(repo), "--format", "json"], env
        )["data"]
    nodes = data.get("nodes") or 0
    if nodes <= 0:
        raise SystemExit(f"FAIL: the frozen engine built an EMPTY graph: {data}")
    print(f"ok: {nodes} nodes, {data.get('files')} files, languages={data.get('languages')}")


def check_memory(carto: Path, root: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp, Offline(carto) as offline:
        repo = Path(tmp)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        run = [*offline.prefix, str(carto)]
        with_model, without = payload_env(root, model=True), payload_env(root, model=False)
        for title, body in MEMORIES:
            added = carto_json(
                [*run, "mem", "add", "--title", title, "--body", body, "--repo", str(repo)],
                with_model,
            )
            if not added.get("ok") or not added["data"]["observation"]["embedded"]:
                raise SystemExit(f"FAIL: mem add did not embed offline: {added}")

        search = [*run, "mem", "search", "--query", REWORDED, "--repo", str(repo)]
        hybrid = carto_json(search, with_model)
        top = (hybrid.get("data") or {}).get("items") or [{}]
        if hybrid.get("search_mode") != "hybrid" or top[0].get("title") != EXPECTED:
            raise SystemExit(f"FAIL: offline hybrid search did not find the memory: {hybrid}")
        print(f"ok: offline hybrid search found {EXPECTED!r} for {REWORDED!r}")

        keyword = carto_json(search, without)
        titles = [i.get("title") for i in (keyword.get("data") or {}).get("items", [])]
        if keyword.get("search_mode") != "keyword" or EXPECTED in titles:
            raise SystemExit(f"FAIL: without the model search must be keyword and miss: {keyword}")
        print(f"ok: without the model: keyword, and missed "
              f"({keyword['data'].get('semantic_unavailable')})")

        measure(run, repo, with_model, without, root)


def measure(run: list[str], repo: Path, with_model, without, root: Path) -> None:
    search = [*run, "mem", "search", "--query", REWORDED, "--repo", str(repo)]
    # Each sample is a fresh process, as every `carto` call is; the OS file
    # cache is warm, because the checks above already read the model.
    rows: dict[str, list[tuple[float, Optional[int]]]] = {"keyword": [], "hybrid": []}
    for _ in range(5):
        rows["keyword"].append(timed(search, without))
        rows["hybrid"].append(timed(search, with_model))

    # Written without the model, then embedded by one sync: what a store that
    # predates the model costs to backfill.
    for n in range(TIMED_ROWS):
        subprocess.run(
            [*run, "mem", "add", "--title", f"timing row {n}",
             "--body", _TIMED_BODY.format(n=n), "--repo", str(repo)],
            env=without, check=True, stdout=subprocess.DEVNULL,
        )
    sync = [*run, "mem", "sync", "--repo", str(repo)]
    embed_s, _ = timed(sync, with_model)
    idle_s, _ = timed(sync, with_model)  # nothing left to embed: the fixed cost
    synced = carto_json([*run, "mem", "status", "--repo", str(repo)], with_model)["data"]
    if synced.get("embedded_observations") != len(MEMORIES) + TIMED_ROWS:
        raise SystemExit(f"FAIL: mem sync did not embed every row: {synced}")

    size = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    model = sum(f.stat().st_size for f in (root / "model").rglob("*") if f.is_file())

    def mb(value: Optional[int]) -> str:
        return "n/a" if value is None else f"{value / 1e6:.0f} MB"

    print("\n== memory search cost on this runner ==")
    built = sorted(Path("dist").glob(f"carto-{root.name}-*.vsix"), key=lambda f: f.stat().st_mtime)
    if built:
        print(f"vsix               {built[-1].name}: {built[-1].stat().st_size / 1e6:.0f} MB")
    print(f"payload            {size / 1e6:.0f} MB unpacked (model {model / 1e6:.0f} MB)")
    for mode, samples in rows.items():
        wall = statistics.median(s for s, _ in samples)
        peaks = [p for _, p in samples if p is not None]
        print(f"mem search {mode:<7} median of 5: {wall:.2f} s, "
              f"peak RSS {mb(max(peaks) if peaks else None)}")
    per_row = (embed_s - idle_s) / TIMED_ROWS * 1000
    print(f"embedding          {per_row:.1f} ms/row ({TIMED_ROWS} rows in one mem sync)")


def main(target: str) -> int:
    root = (Path("dist/payload") / target).resolve()
    manifest = json.loads((root / "PAYLOAD.json").read_text())
    carto = root / manifest["executable"]
    subprocess.run([str(carto), "--version"], check=True)
    check_graph(carto, root)
    check_memory(carto, root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
