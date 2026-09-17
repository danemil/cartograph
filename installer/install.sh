#!/bin/sh
# Install Cartograph for the CLI hosts on a machine with no VS Code.
#
#   ./install.sh --payload /path/to/payload        # or a .vsix, or a .tar.gz
#   ./install.sh --payload ./payload --repo ~/work/app
#
# The VS Code extension is the primary delivery vehicle, and installing it
# already serves Claude Code and Copilot CLI. This script is the same outcome
# for a machine that has neither VS Code nor any wish for it.
#
# **It downloads nothing.** `--payload` names a directory, a `.vsix` or a
# tarball that is already on the machine, because the target machine is behind
# default-deny egress and a fetch here would fail exactly where it hurts most.
# Getting the artifact onto the machine is a separate problem with a separate
# answer per site — a Release download, an internal artifact store, a USB stick.
#
# What it does:
#   1. copies the payload into $CARTO_HOME/payload (default ~/.cartograph)
#   2. writes the launcher, so `carto` resolves on PATH
#   3. runs `carto install` in a repo, if one was named, to place the skills pack
#
# Re-running is safe and is also how you upgrade.
set -eu

CARTO_HOME="${CARTO_HOME:-$HOME/.cartograph}"
PAYLOAD=""
REPO=""
SELF=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

usage() {
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --payload) PAYLOAD="${2:?--payload needs a path}"; shift 2 ;;
        --repo)    REPO="${2:?--repo needs a path}"; shift 2 ;;
        --home)    CARTO_HOME="${2:?--home needs a path}"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "install.sh: unknown argument $1" >&2; usage 1 ;;
    esac
done

[ -n "$PAYLOAD" ] || { echo "install.sh: --payload is required" >&2; usage 1; }
[ -e "$PAYLOAD" ] || { echo "install.sh: no such payload: $PAYLOAD" >&2; exit 1; }

staging=""
# `return 0` is load-bearing, and `set -eu` above is why. Under errexit the EXIT
# trap's own status becomes the script's, so on the directory-payload path —
# where nothing was staged, `[ -n "$staging" ]` is false and `&&` short-circuits
# — a wholly successful install reported failure to whatever automation ran it.
# Reproduced in dash, bash and macOS sh; zsh is the odd one out and returns 0.
# Invisible from a `.vsix`, because there `staging` is set and the `rm` runs,
# and that is the only path anyone had tested by hand.
cleanup() {
    [ -n "$staging" ] && rm -rf "$staging"
    return 0
}
trap cleanup EXIT INT TERM

# A .vsix is a zip whose payload sits under extension/. Accepting one directly
# means the same file serves both hosts, which is the point of building only
# one artifact per platform.
case "$PAYLOAD" in
    *.vsix|*.zip)
        command -v unzip >/dev/null 2>&1 || {
            echo "install.sh: unzip is needed to read $PAYLOAD" >&2; exit 1; }
        staging=$(mktemp -d)
        unzip -q "$PAYLOAD" -d "$staging"
        PAYLOAD="$staging/extension/payload"
        ;;
    *.tar.gz|*.tgz)
        staging=$(mktemp -d)
        tar -xzf "$PAYLOAD" -C "$staging"
        # One directory in, whatever it is called.
        PAYLOAD=$(find "$staging" -maxdepth 2 -name PAYLOAD.json -print -quit | xargs dirname)
        ;;
esac

[ -f "$PAYLOAD/PAYLOAD.json" ] || {
    echo "install.sh: $PAYLOAD has no PAYLOAD.json — is it a Cartograph payload?" >&2
    exit 1
}
[ -x "$PAYLOAD/runtime/carto" ] || [ -f "$PAYLOAD/runtime/carto" ] || {
    echo "install.sh: $PAYLOAD/runtime/carto is missing" >&2; exit 1
}

echo "Installing into $CARTO_HOME"
mkdir -p "$CARTO_HOME/bin"

# Copied, not linked. The payload may have come out of a temporary directory or
# a mounted image, and a link into either is a `carto` that stops working later
# for a reason nobody will connect to this step.
rm -rf "$CARTO_HOME/payload.new"
cp -R "$PAYLOAD" "$CARTO_HOME/payload.new"
rm -rf "$CARTO_HOME/payload.old"
[ -d "$CARTO_HOME/payload" ] && mv "$CARTO_HOME/payload" "$CARTO_HOME/payload.old"
mv "$CARTO_HOME/payload.new" "$CARTO_HOME/payload"
rm -rf "$CARTO_HOME/payload.old"

chmod 0755 "$CARTO_HOME/payload/runtime/carto"
# macOS refuses to exec anything still carrying the download quarantine mark,
# which the whole tree inherits from the file it arrived in.
if [ "$(uname -s)" = "Darwin" ]; then
    xattr -d -r com.apple.quarantine "$CARTO_HOME/payload" 2>/dev/null || true
fi

printf '%s\n' "$CARTO_HOME/payload" > "$CARTO_HOME/runtime.path"

# Both names: the hook lines `carto install` writes guard on `cartograph` being
# resolvable and then invoke `carto`. Shipping one without the other makes
# every hook a silent no-op.
for name in carto cartograph; do
    cp "$SELF/launcher/carto" "$CARTO_HOME/bin/$name"
    chmod 0755 "$CARTO_HOME/bin/$name"
done

echo "Installed $("$CARTO_HOME/bin/carto" --version)"

if [ -n "$REPO" ]; then
    echo "Placing the skills pack in $REPO"
    "$CARTO_HOME/bin/carto" install --platform claude --no-instructions -y --repo "$REPO"
fi

# PATH is the one thing an installer cannot do on the user's behalf without
# editing a file it does not own. Saying so beats a silent half-install.
case ":${PATH}:" in
    *":$CARTO_HOME/bin:"*) ;;
    *)
        echo
        echo "One step left: $CARTO_HOME/bin is not on your PATH."
        echo "Add this to your shell profile, then open a new shell:"
        echo
        echo "    export PATH=\"$CARTO_HOME/bin:\$PATH\""
        ;;
esac
