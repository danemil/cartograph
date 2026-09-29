---
tags: [packaging, cartograph]
updated: 2026-09-17
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
  PAYLOAD.json                   target, engine version, grammar pack version
  runtime/carto[.exe]            the frozen engine (PyInstaller onedir)
  runtime/_internal/…            its libraries
  grammars/tree-sitter-language-pack/v<x.y.z>/libs/…
  grammars/tree-sitter-language-pack/v<x.y.z>/manifest.json
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

Two things that bite:

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
- **Only darwin-arm64 exists.** No linux-x64, linux-arm64 or win32-x64 payload
  has been built, because PyInstaller cannot cross-compile from here.
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
  itself through `carto --version`, which prints `cartograph <x.y.z>` as text.
- **`carto install` is interactive** unless `-y` is passed. Both consumers call
  `carto install --platform copilot --no-instructions -y`, which places
  `.github/skills` and `.github/hooks/cartograph.json` and touches no home
  directory.
- **The engine binary reaching a machine by Release-asset fetch** (T12 §3) is
  not implemented, and the reconciliation already overruled it: the `.vsix`
  carries its own payload. `install.sh` therefore takes `--payload` and
  downloads nothing. Getting the artifact onto the machine is a separate problem
  with a different answer per site.
