#!/usr/bin/env python3
"""Build the per-platform engine payload that the `.vsix` and the installer ship.

    python3 scripts/build-payload.py                 # build for this machine
    python3 scripts/build-payload.py --target darwin-arm64

**This script needs network access. Nothing it produces does.** That is the
whole point: the acceptance test is a machine under default-deny egress, so
every byte `carto` will ever need has to be inside the artifact before it
leaves here.

## The payload layout

Both consumers — `extension/src/payload.ts` and `installer/install.sh` — read
this shape, so it is defined once, here.

    payload/
      PAYLOAD.json                       target, engine version, grammar version
      runtime/carto[.exe]                the frozen engine
      runtime/_internal/…                its libraries
      grammars/tree-sitter-language-pack/v<x.y.z>/libs/…
      grammars/tree-sitter-language-pack/v<x.y.z>/manifest.json

`grammars/` is the surprise, and it is load-bearing.
`tree_sitter_language_pack` 1.x is a Rust extension that **downloads** its
grammar shared libraries on first use and caches them per user. Freezing the
engine does not freeze those: a fresh machine would reach for
github.com on the first `carto build` and, behind default-deny, fail. Pointing
`TREE_SITTER_LANGUAGE_PACK_CACHE_DIR` at a directory seeded here removes that
call entirely. The launcher written by either consumer sets that variable.

## Why the build venv is assembled by hand

`engine/pyproject.toml` still declares `mcp` and `fastmcp` as hard
dependencies — inherited from upstream, where the MCP server was the product.
Cartograph deleted that transport, and `engine/.venv` has run without either
package for the whole project. Installing the project as declared would freeze
the starlette/uvicorn stack into an artifact that never imports it, so the
runtime dependencies are listed explicitly below instead.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"

#: The engine's true runtime dependencies. See the module docstring for why
#: this is not `pip install ./engine`.
RUNTIME_DEPS = [
    "tree-sitter>=0.23.0,<1",
    "tree-sitter-language-pack>=0.3.0,<2",
    "pyyaml>=6.0,<7",
    "networkx>=3.2,<4",
    "watchdog>=4.0.0,<7",
]

#: Pinned so a rebuild months from now produces the same artifact shape.
PYINSTALLER = "pyinstaller==6.16.0"

#: vsce's `--target` vocabulary, which the `.vsix` filenames and the Release
#: asset names both inherit. Keyed by (sys.platform, machine).
_TARGETS = {
    ("darwin", "arm64"): "darwin-arm64",
    ("darwin", "x86_64"): "darwin-x64",
    ("linux", "x86_64"): "linux-x64",
    ("linux", "aarch64"): "linux-arm64",
    ("win32", "AMD64"): "win32-x64",
}


def host_target() -> str:
    key = (sys.platform, platform.machine())
    if key not in _TARGETS:
        raise SystemExit(f"unsupported build host {key}; extend _TARGETS")
    return _TARGETS[key]


def run(argv: list[str], **kw) -> None:
    print("+", " ".join(argv), flush=True)
    subprocess.run(argv, check=True, **kw)


def build_venv() -> Path:
    """A venv holding the engine's runtime deps and PyInstaller, nothing else."""
    venv = BUILD / "payload-venv"
    python = venv / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )
    if not python.exists():
        run([sys.executable, "-m", "venv", str(venv)])
    run([str(python), "-m", "pip", "install", "--upgrade", "pip", "--quiet"])
    run([str(python), "-m", "pip", "install", "--quiet", *RUNTIME_DEPS, PYINSTALLER])
    return python


def freeze(python: Path, out: Path) -> None:
    """Run PyInstaller in onedir mode.

    Onedir, not onefile: a onefile build unpacks the whole tree into a temp
    directory on every invocation, and `carto` is invoked once per query. The
    unpack would dominate the runtime of `carto status`.
    """
    if out.exists():
        shutil.rmtree(out)
    run(
        [
            str(python), "-m", "PyInstaller",
            "--noconfirm", "--clean", "--onedir", "--console",
            "--name", "carto",
            "--distpath", str(out.parent / "_pyinstaller"),
            "--workpath", str(BUILD / "pyinstaller-work"),
            "--specpath", str(BUILD),
            # The engine reaches most of its own modules through function-level
            # imports and `importlib.resources` (the skills pack is package
            # data), so the package is collected wholesale rather than left to
            # import-graph analysis.
            "--collect-all", "cartograph",
            "--collect-all", "tree_sitter_language_pack",
            "--collect-all", "tree_sitter",
            str(ROOT / "scripts" / "carto_entry.py"),
        ],
        env={**os.environ, "PYTHONPATH": str(ROOT / "engine")},
    )
    shutil.move(str(out.parent / "_pyinstaller" / "carto"), str(out))
    shutil.rmtree(out.parent / "_pyinstaller", ignore_errors=True)
    _dereference_symlinks(out)


def _dereference_symlinks(root: Path) -> None:
    """Replace every symlink under *root* with the thing it points at.

    On macOS PyInstaller emits a Python.framework with the usual
    ``Versions/Current`` indirection — symlinks that point at directories. Two
    consumers cannot cope with them:

    * ``vsce`` runs a secret scanner over every packaged file and raises EISDIR
      on the first directory-symlink, failing the package step outright;
    * a ``.vsix`` is a zip, and how a symlink survives a zip round-trip depends
      on the extractor. A payload that is a plain file tree has no such
      question to answer.

    Costs one extra copy of the framework, about 12MB against a 127MB payload.
    """
    while True:
        links = [p for p in root.rglob("*") if p.is_symlink()]
        if not links:
            return
        for link in links:
            target = link.resolve()
            link.unlink()
            if target.is_dir():
                shutil.copytree(target, link, symlinks=False)
            else:
                shutil.copyfile(target, link)
                shutil.copymode(target, link)


def engine_languages(python: Path) -> list[str]:
    """The languages the engine's own extension map can reach.

    Asked of the engine rather than listed here, because a hard-coded copy
    would drift the moment a language is added and the drift would only show up
    as a download attempt on a machine with no egress.
    """
    out = subprocess.run(
        [str(python), "-c",
         "from cartograph.parser import EXTENSION_TO_LANGUAGE as E, "
         "SHEBANG_INTERPRETER_TO_LANGUAGE as S;"
         "print('\\n'.join(sorted(set(E.values()) | set(S.values()))))"],
        check=True, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "engine")},
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def seed_grammars(python: Path, payload: Path) -> str:
    """Download the grammars the engine can use, so the target machine need not.

    Only those: the pack publishes 377 grammars (~495MB of shared libraries),
    against 39 the engine's extension map can actually select. A user-defined
    language in ``languages.toml`` is the one case still needing egress, and
    the pack's own warn-and-skip path handles its absence.

    Returns the pack version, which is part of the cache path the runtime
    computes and therefore has to be recorded.
    """
    cache_base = BUILD / "grammar-cache"
    cache_base.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "TREE_SITTER_LANGUAGE_PACK_CACHE_DIR": str(cache_base)}
    names = engine_languages(python)
    run(
        [
            str(python), "-c",
            # Best-effort per name: the engine's map contains a few entries the
            # pack has no grammar for (`notebook`, `properties`), and one
            # missing name must not fail the build.
            # `get_parser`, not `download`: only the load path materialises the
            # shared library under `libs/`, which is the directory the runtime
            # later looks in. `download` fills `bundles/` and leaves `libs/`
            # empty, which looks like success and ships nothing.
            "import sys, tree_sitter_language_pack as p\n"
            "ok = 0\n"
            "for n in sys.argv[1:]:\n"
            "    try:\n"
            "        p.get_parser(n); ok += 1\n"
            "    except Exception as exc:\n"
            "        print(f'  no grammar for {n}: {type(exc).__name__}')\n"
            "print(f'grammars: {ok}/{len(sys.argv) - 1}')",
            *names,
        ],
        env=env,
    )
    version = subprocess.run(
        [str(python), "-c",
         "import importlib.metadata as m;"
         "print(m.version('tree-sitter-language-pack'))"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()

    src = cache_base / "tree-sitter-language-pack" / f"v{version}"
    if not (src / "libs").is_dir():
        raise SystemExit(f"no grammar libs under {src}; the download produced nothing")
    dest = payload / "grammars" / "tree-sitter-language-pack" / f"v{version}"
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src / "libs", dest / "libs", dirs_exist_ok=True)
    # The runtime reads the manifest to decide a grammar is already present.
    # Without it every lookup is a cache miss and therefore a download attempt.
    shutil.copyfile(src / "manifest.json", dest / "manifest.json")
    return version


def engine_version() -> str:
    text = (ROOT / "engine" / "pyproject.toml").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("version = "):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("could not read version from engine/pyproject.toml")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", default=None,
                    help="vsce target triple; defaults to this machine's")
    ap.add_argument("--skip-grammars", action="store_true",
                    help="Reuse an already-seeded grammar cache under build/")
    args = ap.parse_args()

    target = args.target or host_target()
    if args.target and args.target != host_target():
        # PyInstaller does not cross-compile. Saying so here is cheaper than a
        # broken binary discovered on the target machine.
        raise SystemExit(
            f"cannot build {args.target} on {host_target()}: PyInstaller freezes "
            "the running interpreter. Build each target on its own machine or CI runner."
        )

    payload = DIST / "payload" / target
    if payload.exists():
        shutil.rmtree(payload)
    payload.mkdir(parents=True)

    python = build_venv()
    freeze(python, payload / "runtime")
    grammar_version = seed_grammars(python, payload)

    (payload / "PAYLOAD.json").write_text(
        json.dumps(
            {
                "target": target,
                "engine_version": engine_version(),
                "grammar_pack_version": grammar_version,
                "executable": "runtime/carto.exe" if target.startswith("win32") else "runtime/carto",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    size = sum(f.stat().st_size for f in payload.rglob("*") if f.is_file())
    print(f"\npayload: {payload}  ({size / 1e6:.0f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
