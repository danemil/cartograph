# The acceptance test, as two stages with a network boundary between them.
#
#   docker/acceptance.sh
#
# Stage one has network and builds the payload. Stage two is run with
# `--network none` and installs it. That boundary is the whole claim: every
# byte `carto` will ever need was fetched here, at build time, so the install
# needs nothing.
#
# Stage two's *image* is built with network too — it installs git and jq. Those
# are the harness's own tools, and they stand in for what a developer machine
# already has. The assertions prove Cartograph itself needs neither them nor a
# language runtime, by checking that python, pip, node and npm are all absent.

# ---------------------------------------------------------------- stage one
# Python 3.12 to match the interpreter the engine is developed against;
# PyInstaller freezes whichever one it runs on, so the version is not an
# incidental choice.
#
# Pinned to bookworm, and that matters more than the Python version.
# PyInstaller bundles libpython but NOT libc — the frozen binary dynamically
# links whatever glibc it was built against, and glibc is forward- but not
# backward-compatible. The unpinned `python:3.12-slim` is Debian 13 (glibc
# 2.41) as of this writing, which produces a binary demanding GLIBC_2.38 that
# will not start on Debian 12, RHEL 9, or Ubuntu 22.04 — the machines this is
# actually for. Freeze against the OLDEST glibc the payload must run on.
FROM python:3.12-slim-bookworm AS payload-builder

# PyInstaller reads ELF headers to find the shared libraries it must bundle,
# and shells out to objdump to do it. Without binutils it produces a binary
# that is missing its dependencies and fails only when run.
# curl fetches the embedding model (build-payload.py), as in the CI job.
RUN apt-get update \
    && apt-get install -y --no-install-recommends binutils ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
# Only what build-payload.py reads. The rest of the repo would invalidate this
# layer on every unrelated edit, and the payload takes minutes to rebuild.
COPY engine /work/engine
COPY scripts /work/scripts

# Writes dist/payload/<target>, where target is derived from this container's
# own architecture — linux-x64 under an amd64 platform, linux-arm64 under an
# arm64 one. This is the step that exists because PyInstaller cannot
# cross-compile: a Linux payload has to be frozen on Linux.
RUN python3 scripts/build-payload.py

# ---------------------------------------------------------------- stage two
# Debian slim rather than the Python image: the point is a machine with no
# language runtime on it at all.
FROM debian:bookworm-slim AS acceptance

# git because both the hook line and review-summary ask a repository what
# changed; jq because the assertions read envelopes rather than log text, and
# the container deliberately has no other JSON parser. Neither is a Cartograph
# dependency.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git jq \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged, because the install writes only into $HOME and a test that runs
# as root would not notice if that stopped being true.
RUN useradd --create-home --shell /bin/bash carto

COPY --from=payload-builder /work/dist/payload /opt/payload
COPY installer /opt/installer
# The canonical pack, to compare the installed copies against. The engine
# writes them from package data frozen into the binary, so this is a check on
# the payload rather than on this directory.
COPY skills /opt/canonical-skills
COPY docker/fixture /opt/fixture
COPY docker/assert.sh /opt/assert.sh

USER carto
WORKDIR /home/carto
ENTRYPOINT ["/opt/assert.sh"]
