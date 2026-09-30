---
tags: [packaging, acceptance, cartograph]
updated: 2026-09-30
---

# The acceptance test

`docs/packaging.md` opens by saying the acceptance test is one sentence and
closes by admitting it had never been run. This is the harness that runs it,
and this document is what it proves and what it still cannot.

```bash
docker/acceptance.sh                 # linux/amd64 — the target with no payload yet
docker/acceptance.sh linux/arm64     # native on Apple Silicon; minutes, not an hour
docker/acceptance.sh --keep          # leave the image behind to poke at
docker/acceptance.sh --export        # also copy the tested payload out to dist/
```

Exit zero means every assertion held. Exit non-zero names each one that did not.

## Why a container earns its place here

Two things it does that a stripped-down shell on a developer machine cannot.

**`--network none` is a real default-deny.** What was tested before pointed
`TREE_SITTER_LANGUAGE_PACK_MANIFEST_URL` at a dead port — an imitation, and one
that only blocks the reach it was aimed at. A container with no network
namespace attached has no egress at all, by any route, for any process. The
harness proves that rather than assuming it: no attached interface, an empty
routing table, a refused TCP connect to github.com:443, and a DNS lookup that
does not resolve. Every other claim below rests on those four.

**PyInstaller cannot cross-compile, but a Linux container is Linux.** Stage one
freezes the engine *inside* the container, which is how a linux-x64 or
linux-arm64 payload comes to exist at all from a macOS workstation. This closes
a gap rather than only re-testing an existing artifact.

The two stages, and the boundary between them:

| | network | what it does |
|---|---|---|
| `payload-builder` | **yes** | `scripts/build-payload.py`: freezes the engine, downloads 37 grammars into the payload |
| `acceptance` | **none** | installs that payload with `installer/install.sh` and exercises it |

The network is used at BUILD time so that it is not needed at INSTALL time.
That sentence is the entire product claim, and the boundary is where it is
tested.

## What the test stage has

`debian:bookworm-slim`, unprivileged user, plus `git` and `jq`. Neither is a
Cartograph dependency: `git` is what the hook line and `review-summary` ask
about a repository, and `jq` is the harness's own JSON parser, because the
assertions read envelopes rather than log text and there is nothing else on the
machine to parse with.

That Cartograph needs no language runtime is asserted, not assumed — the run
checks that `python`, `python3`, `pip`, `pip3`, `node` and `npm` are all
absent, so a frozen binary that had quietly started depending on one would
fail here rather than pass by borrowing it.

## What it asserts

On the envelope — `ok`, `error.code`, fields of `data` — never on prose. A
harness that grepped log text would pass on a reworded message and fail on a
rephrased one.

| Claim | Assertion |
|---|---|
| The network is genuinely absent | no attached interface, empty `/proc/net/route`, TCP connect refused, DNS does not resolve |
| Nothing is there to lean on | `python`, `python3`, `pip`, `pip3`, `node`, `npm` all absent |
| A Linux payload can be built at all | `PAYLOAD.json` exists for the container's own target |
| The grammars ship inside it | `*.so` count under `grammars/` > 0 |
| The installer downloads nothing | `install.sh --payload … --repo …` completes with no egress |
| Both names are installed | `carto` and `cartograph` both resolve on `PATH` |
| The frozen engine runs | `carto --version` |
| A fresh repo is a precondition, not a crash | `carto status` → exit 2, `ok:false`, `error.code == "precondition"`, non-empty `error.remediation` |
| **The grammar cache is seeded** | `carto build` exits 0 **and** `status.data.nodes > 0` |
| …and broadly | `status.data.languages` ≥ 10 distinct languages |
| The query protocol answers | `query`, `search`, `review-summary` → `ok:true`, `schema:1`, non-null `data` |
| Search never over-claims | `search_mode` present on the search envelope |
| The catalogue is reachable | `capabilities.data.commands` non-empty |
| Memory round-trips | `mem add` → id; `mem search` returns **that id**, not merely a similar title |
| The embedding model ships | `model/MODEL.json` and `model/model.onnx` in the payload |
| Memory search is meaning-based with no network | `mem add` reports `embedded: true`; a question sharing no searchable word with a recorded decision returns `search_mode: "hybrid"` with that decision first |
| Copilot finds the skills | `.github/skills` is `diff -r` clean against `skills/` |
| The hook fires | the `UserPromptSubmit` command from the generated `.github/hooks/cartograph.json`, run verbatim from the repo with a Copilot payload, exits 0, prints nothing, and the prompt is then in `carto mem search` |
| No MCP is registered | zero files matching `*mcp*` under `$HOME` or the repo |

Two of those are sharper than they look.

**`nodes > 0`, not `exit 0`.** The failure being hunted is a build that
succeeds over an empty graph because every grammar lookup quietly missed. An
exit code cannot tell those apart; a node count can. The fixture is 16 files in
16 languages, so a single fallback path cannot fake it either.

**`mem search` is matched by id.** Matching the title would also pass on a
store that returned some other observation whose text happened to overlap.

## What a real run found

The first two runs failed, which was the point of running them. Both defects
were in the artifact, not in the harness, and neither was reachable by the
stripped-shell method that preceded it — the first needs two different user
accounts, the second two different machines.

### 1. The grammars shipped unreadable

`grammars/tree-sitter-language-pack/v<x>/libs/` shipped as `drwx------`. Every
other directory in the payload is 0755; that one is 0700 because
`tree_sitter_language_pack` creates its cache as a private per-user directory
and `shutil.copytree` preserves the mode. Right for a cache, wrong for a
distributable artifact.

The consequence is worse than a failed copy. A payload built by one account and
installed by another has grammars its own engine cannot read — so every lookup
is a cache miss, and a cache miss is a download attempt, on the one machine
that has no egress. `install.sh`'s `cp -R` happens to fail loudly first, which
is the only reason this surfaced as an error rather than as an empty graph.

It was masked on the `.vsix` path because unzip reassigns ownership to whoever
unpacks, so the artifact worked by accident of ownership rather than by design
— which holds right up until CI builds as root and a user installs as
themselves. Fixed in `scripts/build-payload.py` (`_make_world_readable`), which
widens modes and never narrows them, because narrowing would strip the
executable bit off the frozen engine.

### 2. The binary would not start on an older distribution

With the grammars readable, the install got further and the engine refused to
run at all:

```
Failed to load Python shared library '…/_internal/libpython3.12.so.1.0':
/lib/aarch64-linux-gnu/libm.so.6: version `GLIBC_2.38' not found
```

**PyInstaller bundles libpython but not libc.** The frozen binary dynamically
links whatever glibc it was built against, and glibc is forward- but not
backward-compatible. `python:3.12-slim` had moved to Debian 13 (glibc 2.41), so
the payload demanded `GLIBC_2.38` and could not start on Debian 12 (2.36).

This is not a container artifact. It is the rule for every Linux payload: a
build host newer than the target produces an artifact that does not run, and it
fails at `carto --version` on the fresh machine — precisely where there is no
way to diagnose it. The locked-down machines this project is for are RHEL 9,
Ubuntu 22.04 and Debian 12 far more often than they are Debian 13.

The builder stage is therefore pinned to `python:3.12-slim-bookworm`, and the
rule for any CI runner that builds a Linux payload is the same: **freeze
against the oldest glibc the payload must run on.** The test stage is
deliberately *not* moved up to match the builder — a target that is older than
the build host is the realistic case, and making the two identical would delete
the only assertion that catches this.

### 3. A successful install reported failure

With the engine running, every assertion passed but one: `install.sh` did the
entire install — payload copied, both launchers written, skills placed in all
three directories, hooks written — printed its closing PATH advice, and exited
`1`.

```sh
cleanup() { [ -n "$staging" ] && rm -rf "$staging"; }
trap cleanup EXIT INT TERM
```

`cleanup` runs as the EXIT trap, and a shell takes the trap's own exit status
as the script's. On the directory-payload path nothing is staged, so `staging`
is empty, the `[ -n … ]` test is false, the `&&` short-circuits, and the
function returns 1 — which becomes the script's exit code.

It is invisible from a `.vsix`, where `staging` is set and the `rm` succeeds,
and that is the path `packaging.md` records as tested. It is not a Linux
quirk either: macOS `sh` and `bash` do the same thing. What hid it was that
nobody had run `install.sh` non-interactively from a directory and looked at
`$?` — which is exactly what any CI, wrapper script, or `set -e` caller does.

Fixed with an explicit `return 0` in `cleanup`.

## Recorded runs

Both green, 2026-09-17, on an Apple Silicon host with Docker Desktop 29.8.0.

| Target | Payload | Build | Result |
|---|---|---|---|
| `linux-arm64` | 142 MB, 37 grammars | native, ~25s | all assertions passed |
| `linux-x64` | 136 MB, 37 grammars | QEMU, ~240s | all assertions passed |

Identical graphs on both: 16 files, 16 languages, 50 nodes, 56 edges, with no
network in the container. **`linux-x64` is the target `packaging.md` lists as
having no payload at all**, so this is the first one that has existed.

`linux/amd64` on a non-x86 host needs the emulator registered once:

```bash
docker run --privileged --rm tonistiigi/binfmt --install amd64
```

`acceptance.sh` checks for it and says so rather than letting the build fail
several minutes in with `exec format error`.

2026-09-30, with the embedding model: `linux-arm64`, payload 243 MB, all
assertions passed, including hybrid recall of a reworded memory with no
network. `linux-x64` was not re-run: amd64 emulation is off in this Docker
Desktop, so CI's linux job is the x64 proof.

## What this still cannot verify

Stated plainly, because a confident claim here would be worth less than
nothing.

- **The VS Code extension never activates.** There is no VS Code in the
  container and no window for it to run in. `activate()`, the status bar, the
  `environmentVariableCollection` PATH injection and the version-skew
  notification remain unexercised — the same gap `packaging.md` records. What
  the container does cover is the payload those code paths place and the
  launcher they write, because `install.sh` places the identical tree and the
  launcher is the same shipped file.
- **The `.vsix` itself is not exercised here.** The harness installs from a
  payload directory. `install.sh` accepts a `.vsix` and that path is tested on
  darwin-arm64, but `build-vsix.sh` needs npm and a compile step that this
  Dockerfile deliberately does not carry.
- **Copilot Chat's and Copilot CLI's own discovery** of the placed skills. The
  harness proves the files are in `.github/skills` and byte-identical to
  `skills/`. Whether each host then reads them is a property of the host.
- **Windows.** `installer/install.ps1` has still never been run, and no
  win32-x64 payload exists. A Linux container says nothing about either.
- **The graph's semantic search.** `mem search` is proven hybrid with no
  network (above); the graph's own `search` stays `keyword` in the run, because
  no `carto embed` is run, so that path is untested here.
- **Two of the 39 languages.** The build seeds 37 grammars; the pack has none
  for `notebook` or `vbnet`, and the seeding step skips them by design. A
  repository containing `.ipynb` or `.vb` files would still reach for the
  network on a machine that has none, and fall back to the pack's warn-and-skip
  path. The same is true of any user-defined language in `languages.toml`.
- **Signing and Gatekeeper.** Irrelevant inside a container and unchanged: the
  binary is unsigned.
- **A genuinely fresh machine.** The container is a clean filesystem and a
  clean network namespace, which is most of what "fresh" means and not all of
  it. It shares the host kernel, and `--network none` is an absence of egress
  rather than a policy that denies it — a corporate proxy that intercepts and
  rejects behaves differently from nothing being there at all. The direction of
  that difference is favourable (an interception can only fail a call that
  already succeeds against nothing), but it is not the same test.
- **linux-arm64 is not linux-x64.** Each target must be built and run on its
  own platform; `docker/acceptance.sh` takes the platform as an argument for
  exactly that reason. On a non-x86 host `linux/amd64` runs under QEMU, which
  is a faithful enough userland to freeze and run the binary but is not the
  same as a native x86 machine.
