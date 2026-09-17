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

cd extension
[ -d node_modules ] || npm install --silent
npm run --silent compile

# Staged as real copies rather than links: `vsce` walks the directory and a
# symlink into dist/ would be packaged as a link, not as the 127MB behind it.
rm -rf payload launcher
cp -R "$PAYLOAD" payload
cp -R "$ROOT/installer/launcher" launcher
trap 'rm -rf "$ROOT/extension/payload" "$ROOT/extension/launcher"' EXIT

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
