#!/usr/bin/env bash
# Run the acceptance test: build a Linux payload in a container, then install
# and exercise it in another container that has no network at all.
#
#   docker/acceptance.sh                    # linux/amd64, the target with no payload yet
#   docker/acceptance.sh linux/arm64        # native on Apple Silicon, minutes rather than an hour
#   docker/acceptance.sh --keep             # leave the image behind to poke at
#   docker/acceptance.sh --export           # also copy the payload out to dist/
#
# Two containers and one network boundary. The first has egress and uses it to
# fetch the grammars; the second has `--network none`, which is a real
# default-deny rather than an imitation of one, and is where every claim is
# tested.
#
# On a non-x86 host linux/amd64 runs under QEMU, which is slow but is the only
# way to produce the linux-x64 payload PyInstaller refuses to cross-compile.
set -euo pipefail
cd "$(dirname "$0")/.."

PLATFORM=linux/amd64
KEEP=0
EXPORT=0
for arg in "$@"; do
    case "$arg" in
        --keep) KEEP=1 ;;
        --export) EXPORT=1 ;;
        linux/*) PLATFORM=$arg ;;
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "acceptance.sh: unknown argument $arg" >&2; exit 1 ;;
    esac
done

TAG=cartograph-acceptance:${PLATFORM##*/}

if ! docker info >/dev/null 2>&1; then
    echo "acceptance.sh: the Docker daemon is not running." >&2
    exit 1
fi

# Emulation is not registered by default on an Apple Silicon daemon, and the
# failure without it is `exec format error` from deep inside a build that has
# already run for a while.
host_arch=$(docker info --format '{{.Architecture}}')
case "$PLATFORM:$host_arch" in
    linux/amd64:aarch64|linux/amd64:arm64|linux/arm64:x86_64)
        if ! docker run --rm --platform "$PLATFORM" debian:bookworm-slim true >/dev/null 2>&1; then
            echo "acceptance.sh: this daemon cannot run $PLATFORM images. Register the"
            echo "               emulator first, then re-run:"
            echo
            echo "    docker run --privileged --rm tonistiigi/binfmt --install ${PLATFORM##*/}"
            exit 1
        fi
        ;;
esac

echo "== stage one: build the payload (network allowed) =="
echo "   platform $PLATFORM"
# Plain progress: the interesting part is PyInstaller's and the grammar
# download's own output, and the default renderer collapses it.
docker build \
    --platform "$PLATFORM" \
    --target acceptance \
    --progress plain \
    -f docker/Dockerfile \
    -t "$TAG" \
    .

echo
echo "== stage two: install and exercise it (--network none) =="
status=0
docker run --rm \
    --platform "$PLATFORM" \
    --network none \
    "$TAG" || status=$?

if [ "$EXPORT" -eq 1 ]; then
    # The container is the only way to produce a Linux payload from a macOS
    # workstation, so the artifact is worth keeping once it has been tested.
    # Copied out of a stopped container rather than bind-mounted in, so the
    # build itself stays hermetic.
    echo
    cid=$(docker create --platform "$PLATFORM" "$TAG")
    mkdir -p dist/payload
    docker cp "$cid:/opt/payload/." dist/payload/
    docker rm -f "$cid" >/dev/null
    echo "exported:"
    find dist/payload -maxdepth 2 -name PAYLOAD.json -exec dirname {} \;
fi

if [ "$KEEP" -eq 0 ]; then
    docker image rm -f "$TAG" >/dev/null 2>&1 || true
else
    echo
    echo "image kept as $TAG — a shell into it:"
    echo "    docker run --rm -it --network none --entrypoint /bin/bash $TAG"
fi

exit "$status"
