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
      model/MODEL.json                   what the model is, where it came from
      model/model.onnx, tokenizer.json   the embedding model memory search runs

`grammars/` is the surprise, and it is load-bearing.
`tree_sitter_language_pack` 1.x is a Rust extension that **downloads** its
grammar shared libraries on first use and caches them per user. Freezing the
engine does not freeze those: a fresh machine would reach for
github.com on the first `carto build` and, behind default-deny, fail. Pointing
`TREE_SITTER_LANGUAGE_PACK_CACHE_DIR` at a directory seeded here removes that
call entirely. The launcher written by either consumer sets that variable.

`model/` is the same idea for memory search: a sentence-embedding model,
fetched here at a pinned revision and checked by sha256, run at query time by
ONNX Runtime on the CPU. The launcher points ``CARTO_EMBEDDING_MODEL_DIR`` at
it the way it points the grammar variable. ``--model-only`` fetches just the
model into ``build/model`` — what the engine's tests use.

## The build interpreter

The frozen engine inherits the build interpreter's ``sqlite3``. sqlite-vec is a
loadable extension, and an interpreter built without
``enable_load_extension`` — the python.org macOS installer is one — freezes
into a payload that carries sqlite-vec and can never load it. So the
interpreter is checked before anything is built: the running one if it can,
else uv's CPython 3.12 if uv is installed, else ``--python``, else the build
stops.

## Why the build venv is assembled by hand

`engine/pyproject.toml` declared `mcp` and `fastmcp` as hard dependencies
until the MCP server was removed — inherited from upstream, where that server
was the product. Installing the project as declared would have frozen the
starlette/uvicorn stack into an artifact that never imports it, so the runtime
dependencies are listed explicitly below. The list matches what the project
declares now; a dependency added there reaches the artifact only once it is
added here too.
"""

from __future__ import annotations

import argparse
import hashlib
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
    # Memory search's vector path. Pinned exactly: these are native code whose
    # size and platform coverage were measured (docs/memory-design.md), and a
    # silent minor bump is how a payload grows by 30 MB or loses a platform.
    "sqlite-vec==0.1.9",
    "onnxruntime==1.30.0",
    "numpy==2.5.3",
]

#: Installed without its dependencies: tokenizers declares huggingface_hub,
#: the hub *client*, which the engine never imports (it builds the tokenizer
#: from a file) and which would carry an HTTP stack into an artifact that must
#: not reach the network.
NO_DEPS = ["tokenizers==0.23.2"]

#: Pinned so a rebuild months from now produces the same artifact shape.
PYINSTALLER = "pyinstaller==6.16.0"

#: The embedding model the payload carries (decision 2): all-MiniLM-L6-v2,
#: Apache-2.0, dynamically quantised to int8. Pinned to a commit of the model
#: repository and checked by sha256, so a rebuild ships the same bytes or
#: fails. The file is named for arm64 upstream, but its bytes are identical to
#: the repository's `qint8_avx512` and `qint8_avx512_vnni` variants (same
#: sha256): one signed-int8 model that ONNX Runtime runs on any CPU. Against
#: the fp32 model its vectors measured cosine >= 0.993 on the same sentences.
MODEL = {
    "name": "all-MiniLM-L6-v2-qint8",
    "source": "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2",
    "revision": "1110a243fdf4706b3f48f1d95db1a4f5529b4d41",
    "license": "Apache-2.0",
    "dimension": 384,
    "max_seq_length": 256,
    "model": "model.onnx",
    "tokenizer": "tokenizer.json",
    "files": {
        "model.onnx": {
            "from": "onnx/model_qint8_arm64.onnx",
            "sha256": "4278337fd0ff3c68bfb6291042cad8ab363e1d9fbc43dcb499fe91c871902474",
        },
        "tokenizer.json": {
            "from": "tokenizer.json",
            "sha256": "be50c3628f2bf5bb5e3a7f17b1f74611b2561a3a27eeab05e5aa30f411572037",
        },
    },
}

#: ONNX Runtime's standalone C-API library. The Python module is linked
#: without it on all three platforms (checked with otool, the ELF dynamic
#: section, and the PE import table of the 1.30.0 wheels), so it is 18-33 MB of
#: payload nothing loads. The frozen smoke test embeds without it.
_ORT_UNUSED = ("libonnxruntime.*.dylib", "libonnxruntime.so.*", "onnxruntime.dll")

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


def _loads_extensions(base: str) -> bool:
    probe = subprocess.run(
        [base, "-c", "import sqlite3; sqlite3.connect(':memory:').enable_load_extension(True)"],
        capture_output=True, text=True,
    )
    return probe.returncode == 0


def choose_interpreter(requested: "str | None") -> str:
    """The interpreter to freeze: one whose sqlite3 can load extensions.

    See the module docstring. Checked on the interpreter itself because the
    venv built from it shares its ``_sqlite3``, and so will the frozen engine.
    Named with ``--python``, it must pass or the build stops. Otherwise the
    running interpreter is used when it passes, and a uv-managed CPython 3.12
    when it does not — said out loud, because it changes what gets frozen.
    """
    if requested:
        if not _loads_extensions(requested):
            raise SystemExit(
                f"{requested}: its sqlite3 cannot load extensions, so the payload's "
                "sqlite-vec could never load and memory search would stay keyword-only."
            )
        return requested
    if _loads_extensions(sys.executable):
        return sys.executable
    uv = shutil.which("uv")
    if uv:
        found = subprocess.run(
            [uv, "python", "find", "--system", "--managed-python", "3.12"], capture_output=True, text=True,
        ).stdout.strip()
        if found and _loads_extensions(found):
            print(f"{sys.executable} cannot load SQLite extensions; freezing {found} instead")
            return found
    raise SystemExit(
        f"{sys.executable}: its sqlite3 cannot load extensions, so the payload's sqlite-vec "
        "could never load and memory search would stay keyword-only. Install one that "
        "can (`uv python install 3.12`) or name it with --python."
    )


def build_venv(base: str) -> Path:
    """A venv holding the engine's runtime deps and PyInstaller, nothing else."""
    venv = BUILD / "payload-venv"
    python = venv / ("Scripts" if os.name == "nt" else "bin") / (
        "python.exe" if os.name == "nt" else "python"
    )
    marker = venv / "base-interpreter"
    if python.exists() and (not marker.exists() or marker.read_text() != base):
        # A venv made from another interpreter keeps that one's sqlite3; reusing
        # it would quietly undo the check above.
        shutil.rmtree(venv)
    if not python.exists():
        run([base, "-m", "venv", str(venv)])
        marker.write_text(base)
    run([str(python), "-m", "pip", "install", "--upgrade", "pip", "--quiet"])
    run([str(python), "-m", "pip", "install", "--quiet", *RUNTIME_DEPS, PYINSTALLER])
    run([str(python), "-m", "pip", "install", "--quiet", "--no-deps", *NO_DEPS])
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
            # sqlite-vec is a shared library the package loads by path, which
            # import analysis cannot see. The embedding stack is imported only
            # inside functions, so it is named rather than left to be found.
            "--collect-all", "sqlite_vec",
            "--hidden-import", "onnxruntime",
            "--hidden-import", "tokenizers",
            "--hidden-import", "numpy",
            # Reachable from onnxruntime's optional tooling, never from the
            # inference path: model conversion, quantisation and training.
            "--exclude-module", "onnxruntime.transformers",
            "--exclude-module", "onnxruntime.quantization",
            "--exclude-module", "onnxruntime.tools",
            "--exclude-module", "onnxruntime.training",
            "--exclude-module", "sympy",
            "--exclude-module", "huggingface_hub",
            str(ROOT / "scripts" / "carto_entry.py"),
        ],
        env={**os.environ, "PYTHONPATH": str(ROOT / "engine")},
    )
    shutil.move(str(out.parent / "_pyinstaller" / "carto"), str(out))
    shutil.rmtree(out.parent / "_pyinstaller", ignore_errors=True)
    _dereference_symlinks(out)
    for pattern in _ORT_UNUSED:
        for unused in out.rglob(pattern):
            print(f"  pruned {unused.relative_to(out)} ({unused.stat().st_size / 1e6:.0f} MB)")
            unused.unlink()
    if sys.platform.startswith("linux"):
        _drop_duplicate_libs(out / "_internal")


def _drop_duplicate_libs(internal: Path) -> None:
    """Remove top-level copies of libraries a wheel vendors in ``<pkg>.libs/``.

    PyInstaller's dependency scan copies each vendored library (NumPy's 27 MB
    OpenBLAS) to the top of ``_internal`` as well as keeping ``numpy.libs/``.
    The extension modules find theirs through ``DT_RPATH $ORIGIN/../../numpy.libs``,
    which the loader honours before anything else, so the top-level copy is
    never loaded. Linux only: that is where the RPATH was read and the result
    run (docker/acceptance.sh); Windows' delvewheel layout was not checked.
    """
    for vendored in internal.glob("*.libs/*"):
        twin = internal / vendored.name
        if twin.is_file() and twin.stat().st_size == vendored.stat().st_size \
                and _sha256(twin) == _sha256(vendored):
            print(f"  dropped duplicate {twin.name} ({twin.stat().st_size / 1e6:.0f} MB)")
            twin.unlink()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch_model(dest: Path) -> Path:
    """Download the embedding model into *dest*, verified; reuse it if already there.

    A file whose hash does not match is an error, not a warning: the pinned
    revision is what the licence record and the measurements describe.
    """
    dest.mkdir(parents=True, exist_ok=True)
    for name, spec in MODEL["files"].items():
        target = dest / name
        if target.exists() and _sha256(target) == spec["sha256"]:
            continue
        url = f"{MODEL['source']}/resolve/{MODEL['revision']}/{spec['from']}"
        partial = target.with_suffix(target.suffix + ".part")
        # curl rather than urllib: every build host has it (the Linux job
        # installs it), and it uses the system's CA store, where a python.org
        # interpreter ships none until someone runs its certificate installer.
        run(["curl", "-fsSL", "--retry", "3", "-o", str(partial), url])
        got = _sha256(partial)
        if got != spec["sha256"]:
            partial.unlink()
            raise SystemExit(f"{url}: sha256 {got}, expected {spec['sha256']}")
        partial.replace(target)
    manifest = {key: value for key, value in MODEL.items() if key != "files"}
    manifest["files"] = {
        name: {"sha256": spec["sha256"], "size": (dest / name).stat().st_size,
               "from": spec["from"]}
        for name, spec in MODEL["files"].items()
    }
    (dest / "MODEL.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return dest


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


def _make_world_readable(root: Path) -> int:
    """Widen every mode under *root* so a user who did not build it can read it.

    ``tree_sitter_language_pack`` creates its cache as a *private* per-user
    directory: ``libs/`` comes out 0700. Right for a cache, wrong for a
    payload, which is a distributable artifact — ``copytree`` preserves the
    mode, so the grammars end up readable only by the account that ran this
    script. The engine then finds an unreadable cache, treats every lookup as a
    miss and reaches for the network on the one machine that has none; and
    ``install.sh``'s ``cp -R`` fails outright when the installing user is not
    the building user.

    It is masked on the ``.vsix`` path, where unzip reassigns ownership to
    whoever unpacks — so the artifact survives by accident of ownership rather
    than by design, which holds right up until CI builds as root and a user
    installs as themselves.

    Only ever widens. Narrowing here would strip the executable bit off the
    frozen engine, which is the other way to ship a payload that cannot run.
    """
    widened = 0
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        mode = path.stat().st_mode & 0o7777
        # Read for everyone; traverse as well, but only on directories.
        wider = mode | (0o055 if path.is_dir() else 0o044)
        if wider != mode:
            path.chmod(wider)
            widened += 1
    return widened


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
    ap.add_argument("--python", default=None,
                    help="Interpreter to freeze (its sqlite3 must load extensions; "
                         "default: this one, else uv's CPython 3.12)")
    ap.add_argument("--model-only", action="store_true",
                    help="Only fetch and verify the embedding model into build/model")
    args = ap.parse_args()

    if args.model_only:
        print(f"model: {fetch_model(BUILD / 'model')}")
        return 0

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

    python = build_venv(choose_interpreter(args.python))
    freeze(python, payload / "runtime")
    grammar_version = seed_grammars(python, payload)
    shutil.copytree(fetch_model(BUILD / "model"), payload / "model")

    (payload / "PAYLOAD.json").write_text(
        json.dumps(
            {
                "target": target,
                "engine_version": engine_version(),
                "grammar_pack_version": grammar_version,
                "embedding_model": MODEL["name"],
                "executable": "runtime/carto.exe" if target.startswith("win32") else "runtime/carto",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # Last, so it covers everything every step above put there.
    widened = _make_world_readable(payload)

    size = sum(f.stat().st_size for f in payload.rglob("*") if f.is_file())
    print(f"\npayload: {payload}  ({size / 1e6:.0f} MB, {widened} modes widened)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
