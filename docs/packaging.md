---
tags: [packaging, cartograph]
updated: 2026-09-30
---

# Packaging

How the engine reaches a machine that has nothing on it.

The acceptance test is one sentence: **a fresh machine with nothing but VS Code,
behind default-deny egress, installs Cartograph and it works.** Copilot ships
inside VS Code 1.135.0, so the `.vsix` is the only artifact that reaches such a
machine — and installing it therefore has to serve Copilot CLI too, which VS
Code knows nothing about.

## The payload

One directory, built per platform, consumed by both the `.vsix` and the
installer. `scripts/build-payload.py` produces it and is the only definition
that matters; this is a summary.

```
payload/
  PAYLOAD.json                   target, release (engine_version), upstream engine, grammar pack version
  runtime/carto[.exe]            the frozen engine (PyInstaller onedir)
  runtime/_internal/…            its libraries
  grammars/tree-sitter-language-pack/v<x.y.z>/libs/…
  grammars/tree-sitter-language-pack/v<x.y.z>/manifest.json
  model/MODEL.json               the embedding model: name, source, revision, licence, sha256s
  model/model.onnx               all-MiniLM-L6-v2, int8 (23 MB)
  model/tokenizer.json
```

`grammars/` is the part nobody expected to need.

**`tree_sitter_language_pack` 1.x does not ship grammars.** It is a 4.7MB Rust
extension that *downloads* grammar shared libraries on first use and caches them
per user, under `TREE_SITTER_LANGUAGE_PACK_CACHE_DIR`. Freezing the engine does
not freeze those. A fresh machine would reach for github.com on the first
`carto build` and, behind default-deny, get nothing — the design record's
"grammars are precompiled, never built at runtime" solved the *C toolchain*
problem and left the *egress* problem untouched.

So the build seeds that cache and the launcher points at it. 37 grammars, 92MB —
the pack publishes 377 (~495MB), against 39 the engine's extension map can
actually select, so only those are fetched. The list is asked of the engine, not
written down here, because a written copy would drift and the drift would only
appear as a failed download on a machine that cannot download.

A user-defined language in `languages.toml` is the one remaining case that needs
egress. The pack's warn-and-skip path handles its absence.

`model/` is the same arrangement for memory search (decision 2). The model is
fetched at build time from a pinned revision and checked by sha256
(`MODEL` in `build-payload.py`), and the launchers and the extension point
`CARTO_EMBEDDING_MODEL_DIR` at it exactly as they point
`TREE_SITTER_LANGUAGE_PACK_CACHE_DIR` at the grammars; an operator's own value
wins. Unset, or pointing at nothing, and `mem search` answers `keyword` and
says which. The runtime — ONNX Runtime, Hugging Face `tokenizers`, NumPy,
sqlite-vec — is frozen into `runtime/` with the engine; no PyTorch. The
standalone ONNX Runtime C library (18–33 MB by platform) is pruned, because
the Python module does not link it on any of the three platforms. Measured
cost: `docs/memory-design.md`.

## Installed layout

Both the extension and `installer/install.sh` produce this, and the launcher is
the same file in both cases — shipped, never regenerated.

```
$CARTO_HOME              (default ~/.cartograph, the engine's own state dir)
  bin/carto              launcher; `bin/` goes on PATH
  bin/cartograph         the same file under the other name
  runtime.path           absolute path of the payload currently in use
```

Two decisions worth knowing:

- **The launcher follows a pointer file** rather than having a path baked in. A
  VS Code extension lives in a version-stamped directory, so a baked path would
  leave Copilot CLI and the Chat hooks aimed at a directory the next extension
  update deleted — failing later, elsewhere, for a reason nobody would connect
  back to this.
- **Both names are installed.** `carto` is what every hook, skill and
  remediation string invokes; `cartograph` is the spelled-out alias, kept so an
  install or hook written before the rename still resolves.

  The guard and the invocation must always name the SAME binary. They did not
  once — hooks tested `command -v cartograph` and then ran `carto`, so an
  install placing only `carto` made every hook exit 0 having done nothing. That
  is the failure mode this project is least able to detect: nothing errors,
  nothing is logged, the graph simply stops updating. Shipping both names costs
  a symlink and removes the question.

## Building

```bash
python3 scripts/build-payload.py          # needs network; the result needs none
./scripts/build-vsix.sh                   # → dist/carto-<target>-<version>.vsix
```

Three things that bite:

- **The build interpreter must be able to load SQLite extensions.** The frozen
  engine uses that interpreter's `sqlite3`, and the python.org macOS build has
  no `enable_load_extension`: a payload frozen from it carries sqlite-vec and
  can never load it. `build-payload.py` checks, falls back to uv's CPython 3.12
  when the running interpreter fails, and stops if neither will do. CI's macOS
  job installs uv for this; the Linux image and Windows' python.org build pass.
- **A Linux build needs `cargo`**, and an old build image — see
  "Supported Linux". `build-payload.py` stops without cargo rather than let pip
  fall back to a wheel that raises the glibc floor.
- **PyInstaller does not cross-compile.** It freezes the interpreter it is
  running on, so each target is built on its own machine or CI runner. The
  script refuses a `--target` that is not the host rather than emitting a
  binary that cannot run.
- **No symlinks may survive into the payload.** PyInstaller's macOS output
  contains a `Python.framework` with the usual `Versions/Current` indirection.
  `vsce` runs a secret scanner over every packaged file and raises `EISDIR` on
  the first directory-symlink, failing the package step; and how a symlink
  survives a zip round-trip depends on the extractor. `build-payload.py`
  dereferences them, costing about 12MB.

## Supported Linux

**Floor: glibc 2.31** — Ubuntu 20.04, Debian 11, and anything newer; RHEL /
Alma / Rocky 8 (glibc 2.28) are expected to work, because the payload measures
lower than the floor, but are not run.

PyInstaller bundles libpython, libstdc++ and the rest but never libc, so each
ELF file in the payload needs the glibc it was linked against, and the newest
of those is the payload's floor. 0.8.0 was built on Debian 12 and bundled its
`libstdc++.so.6` (needs `GLIBC_2.36`): on Ubuntu 22.04 (2.35) the graph ran and
NumPy, which loads that libstdc++ through ONNX Runtime, did not. No proof had
run on anything older than Debian 12.

How the floor is kept:

- **Built on `quay.io/pypa/manylinux_2_28`** (AlmaLinux 8, glibc 2.28), with
  uv's CPython 3.12 frozen — the image's own CPython is static, and PyInstaller
  needs a shared libpython. Debian 11 was the first choice and is unusable:
  it left LTS on 2026-08-31 and its security mirror lists packages it no
  longer serves, so `apt-get install` fails.
- **`tree-sitter-language-pack` is compiled from source on Linux**
  (`_SOURCE_ON_LINUX` in `build-payload.py`; needs `cargo`). Every 1.x Linux
  wheel is `manylinux_2_34`: `dlopen`, `pthread_create` and friends bind to
  their `GLIBC_2.34` versions. That one file would otherwise set the floor at
  2.34 on any build image. The other native wheels are fine as published:
  onnxruntime 1.30.0 needs 2.28, numpy 2.5.3 2.27, tokenizers 2.16,
  sqlite-vec and tree-sitter 2.14, the downloaded grammars 2.14.
- **Asserted on every build.** `scripts/check-glibc.py` reads each ELF file's
  version-needs section and fails above `--max` (2.31); CI prints the ten
  highest to the job summary, so a raised floor names its file.
- **Proven on the targets.** CI's `linux-targets` jobs unzip the shipped
  `.vsix` inside plain `ubuntu:22.04`, `ubuntu:20.04` and `debian:11`
  containers (no Python; git and a standalone harness interpreter only) and run
  `scripts/ci_smoke.py`: a graph build, then hybrid memory search finding a
  reworded memory with the network blocked by `unshare --net`.

Evidence, 2026-09-30:

| Where | Payload max | Result |
|---|---|---|
| linux-arm64, local Docker (manylinux_2_28 build) | `GLIBC_2.28` (tree-sitter-language-pack, onnxruntime) | ci_smoke passed offline on `ubuntu:20.04` and `debian:11` |
| linux-x64, CI run [36713397573](https://github.com/danemil/cartograph/actions/runs/36713397573), build job | `GLIBC_2.28` (same two files), 58 ELF files | check passed; ci_smoke passed on the AlmaLinux 8.10 build host (2.28) |
| same run, `ubuntu:22.04` (2.35) | — | shipped `.vsix` passed offline: hybrid 0.50 s / 144 MB, keyword 0.35 s |
| same run, `ubuntu:20.04` (2.31) | — | passed offline: hybrid 0.61 s / 144 MB, keyword 0.44 s |
| same run, `debian:11` (2.31) | — | passed offline: hybrid 0.58 s / 144 MB, keyword 0.40 s |

Embedding cost on those runners ranged 9–23 ms/row (shared CI machines; the
spread is the runner, not the image).

## What is verified, and how

On darwin-arm64, with `env -i`, an empty `HOME`, a minimal `PATH`, and
`TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL` pointed at a dead port so any attempt
to reach for a grammar fails loudly:

| Claim | How |
|---|---|
| The frozen engine parses | `carto build` on a 3-file repo: 7 nodes, 6 edges, 3 languages — identical to the unfrozen engine on the same repo |
| The `.vsix` builds | `scripts/build-vsix.sh` → 32MB compressed, 146MB unpacked |
| VS Code accepts it | `code --install-extension` into a throwaway `--extensions-dir`; the executable bit survived the unpack on this build |
| The payload survives the zip | `install.sh --payload <the .vsix>`, then a build from the unzipped engine |
| The extension's placement code works | `readPayload` / `placeLaunchers` run under plain node against the VS Code-installed directory, then the launcher they wrote used for a real build |
| Hosts find the skills | the 15 installed `SKILL.md` copies compared byte-for-byte against `skills/` |
| No MCP is registered | no `.mcp.json` written by `carto install` |

## What is NOT verified

Stated plainly, because a confident claim here would be worth less than nothing.

- **The acceptance test itself has never been run.** No fresh machine, no real
  default-deny network. What was tested is an environment stripped to look like
  one, on a developer machine that does have egress.
- **The extension has never activated inside a VS Code window.** `activate()`,
  the status bar, the terminal `PATH` injection through
  `environmentVariableCollection`, and the version-skew notification are all
  unexercised. The code they call is not.
- **Only the Linux payload has run on a target's own OS.** linux-x64 is proven
  on Ubuntu 20.04/22.04 and Debian 11 containers in CI (above); win32-x64 and
  darwin-arm64 only on their CI runners and this Mac.
- **`installer/install.ps1` has never been run.** There is no Windows machine
  and no Windows payload. It was written against the same contract `install.sh`
  implements, and that is all that can be said for it.
- **The binary is unsigned.** The installer clears the macOS quarantine mark,
  which is not the same as being signed or notarised; a stricter Gatekeeper
  configuration will still refuse it. That needs a signing identity, not code.
- **Copilot Chat's own discovery** of the placed skills was not re-tested here.
  It is the smoke test recorded in the reconciliation, not a result of this work.

## Where the design record disagrees with the shipped CLI

- **`carto version --json` does not exist** (T16 §6). The binary identifies
  itself through `carto --version`, which prints text:
  `cartograph 0.8.4 (engine fork of code-review-graph 2.3.8)`. The first
  version is the Cartograph release — `extension/package.json`, the one file a
  release bump edits; `build-payload.py` stamps it into the frozen bundle
  (`cartograph/RELEASE`) and into `PAYLOAD.json` as `engine_version`, and
  `build-vsix.sh` refuses a payload stamped with another release. The second is
  the upstream release the engine forked from (`cartograph.__version__`,
  `pyproject.toml`). Releases up to 0.8.4 printed only the upstream version.
- **`carto install` is interactive** unless `-y` is passed. Both consumers call
  `carto install --platform copilot --no-instructions -y`, which places
  `.github/skills` and `.github/hooks/cartograph.json` and touches no home
  directory.
- **The engine binary reaching a machine by Release-asset fetch** (T12 §3) is
  not implemented, and the reconciliation already overruled it: the `.vsix`
  carries its own payload. `install.sh` therefore takes `--payload` and
  downloads nothing. Getting the artifact onto the machine is a separate problem
  with a different answer per site.
