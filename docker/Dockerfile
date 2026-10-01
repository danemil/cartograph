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
# The same image and interpreter as CI's linux job (.github/workflows/release.yml),
# which explains the choice at length. In short: PyInstaller bundles libpython
# and libstdc++ but NOT libc, so the payload needs the glibc of whatever built
# it — and the machine it lands on is older than any developer's. Pinned to
# bookworm (2.36), 0.8.0's bundled libstdc++ could not load on Ubuntu 22.04
# (2.35). manylinux_2_28 is glibc 2.28 and maintained for building exactly
# this kind of artifact. Its own CPython is static and PyInstaller needs a
# shared libpython, so uv's CPython 3.12 is frozen instead — which also loads
# SQLite extensions, as build-payload.py requires.
#
# The image carries binutils (PyInstaller shells out to objdump), curl (the
# embedding model) and uv. Rust compiles tree-sitter-language-pack, whose only
# Linux wheel needs glibc 2.34.
ARG TARGETARCH
FROM quay.io/pypa/manylinux_2_28_x86_64 AS builder-amd64
FROM quay.io/pypa/manylinux_2_28_aarch64 AS builder-arm64
FROM builder-${TARGETARCH} AS payload-builder

RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal \
    && uv python install 3.12 \
    && ln -s "$(uv python find --managed-python 3.12)" /usr/local/bin/build-python
ENV PATH=/root/.cargo/bin:$PATH

WORKDIR /work
# Only what build-payload.py reads. The rest of the repo would invalidate this
# layer on every unrelated edit, and the payload takes minutes to rebuild.
COPY engine /work/engine
COPY scripts /work/scripts
# The release the payload is stamped with; see engine/cartograph/release.py.
COPY extension/package.json /work/extension/package.json

# Writes dist/payload/<target>, where target is derived from this container's
# own architecture — linux-x64 under an amd64 platform, linux-arm64 under an
# arm64 one. This is the step that exists because PyInstaller cannot
# cross-compile: a Linux payload has to be frozen on Linux. Then the floor:
# no ELF file in the payload may need a glibc newer than 2.31.
RUN build-python scripts/build-payload.py --python "$(readlink -f /usr/local/bin/build-python)" \
    && build-python scripts/check-glibc.py dist/payload/linux-* --max 2.31

# ---------------------------------------------------------------- stage two
# A plain distribution image rather than a Python one: the point is a machine
# with no language runtime on it at all. Ubuntu 20.04 (glibc 2.31) because it
# is the oldest the payload claims (docs/packaging.md, "Supported Linux"); a
# test stage older than the builder's claims is the only one that catches a
# floor that crept up.
FROM ubuntu:20.04 AS acceptance
ENV DEBIAN_FRONTEND=noninteractive

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
