#!/usr/bin/env bash
# Package the VS Code extension, with this platform's engine inside it.
#
#   ./scripts/build-vsix.sh                  # this machine's target
#   ./scripts/build-vsix.sh darwin-arm64
#
# Needs network the first time (npm, and `build-payload.py`'s own downloads).
# The `.vsix` it produces needs none: that is the acceptance test.
#
# One `.vsix` per platform, each carrying one binary — `vsce package --target`
# is what makes that a supported shape rather than a trick. A single fat file
# with every platform's binary would be five times the size for every user.
set -euo pipefail
cd "$(dirname "$0")/.."

ROOT=$PWD
TARGET=${1:-}
if [ -z "$TARGET" ]; then
    TARGET=$(python3 -c 'import sys; sys.path.insert(0, "scripts"); \
import importlib.util as u; s = u.spec_from_file_location("b", "scripts/build-payload.py"); \
m = u.module_from_spec(s); s.loader.exec_module(m); print(m.host_target())')
fi

PAYLOAD="$ROOT/dist/payload/$TARGET"
if [ ! -f "$PAYLOAD/PAYLOAD.json" ]; then
    echo "No payload for $TARGET. Build it first:"
    echo "    python3 scripts/build-payload.py --target $TARGET"
    exit 1
fi
# A payload older than the engine source would be packaged without a word, and
# the .vsix would then ship an engine missing whatever changed since. That
# happened: a local 0.2.0 build carried a payload from twelve days earlier.
STALE=$(find "$ROOT/engine/cartograph" -name '*.py' -newer "$PAYLOAD/PAYLOAD.json" | head -1)
if [ -n "$STALE" ]; then
    echo "The $TARGET payload is older than the engine source (e.g. ${STALE#$ROOT/})."
    echo "Rebuild it first:"
    echo "    python3 scripts/build-payload.py --target $TARGET"
    exit 1
fi
# The frozen engine reports the release it was stamped with. A bump after the
# payload was built would ship a .vsix whose own skew check warns on first run.
# Read from inside each directory: on Windows, Git Bash hands node a /d/a/...
# path it cannot open, and only a relative require resolves the same everywhere.
PAYLOAD_RELEASE=$(cd "$PAYLOAD" && node -p "require('./PAYLOAD.json').engine_version")
PACKAGE_RELEASE=$(cd "$ROOT/extension" && node -p "require('./package.json').version")
if [ "$PAYLOAD_RELEASE" != "$PACKAGE_RELEASE" ]; then
    echo "The $TARGET payload was stamped $PAYLOAD_RELEASE; extension/package.json is $PACKAGE_RELEASE."
    echo "Rebuild it first:"
    echo "    python3 scripts/build-payload.py --target $TARGET"
    exit 1
fi

cd extension
[ -d node_modules ] || npm install --silent
npm run --silent compile

# Cartograph Local rides inside every platform's .vsix, so a remote window can
# offer to install it without anyone fetching a second file first.
"$ROOT/scripts/build-companion.sh" >/dev/null
COMPANION_VERSION=$(node -p "require('./package.json').version")

# Staged as real copies rather than links: `vsce` walks the directory and a
# symlink into dist/ would be packaged as a link, not as the 127MB behind it.
rm -rf payload launcher companion
cp -R "$PAYLOAD" payload
cp -R "$ROOT/installer/launcher" launcher
mkdir companion
cp "$ROOT/dist/cartograph-local-$COMPANION_VERSION.vsix" companion/cartograph-local.vsix
trap 'rm -rf "$ROOT/extension/payload" "$ROOT/extension/launcher" "$ROOT/extension/companion"' EXIT

VERSION=$(node -p "require('./package.json').version")
OUT="$ROOT/dist/carto-$TARGET-$VERSION.vsix"
mkdir -p "$ROOT/dist"

# --allow-missing-repository: private monorepo, so there is no public
# repository URL and vsce's warning is about a marketplace listing that will
# never exist.
#
# statement about secrets: the scanner reads every packaged file, and
# PyInstaller's macOS output contains framework symlinks pointing at
# directories (`Python.framework/Versions/Current`). Reading one raises EISDIR
# and fails the whole package step.
npx --yes @vscode/vsce@3 package \
    --target "$TARGET" \
    --out "$OUT" \
    --allow-missing-repository \
    --skip-license

echo
ls -lh "$OUT"
