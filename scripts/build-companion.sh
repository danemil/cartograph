#!/usr/bin/env bash
# Package Cartograph Local, the companion that runs on the local side of a
# remote window. Plain JavaScript and no engine, so one .vsix serves every
# platform — no --target.
#
#   ./scripts/build-companion.sh        # -> dist/cartograph-local-<version>.vsix
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Compiled with the main extension's TypeScript and type packages, so the
# companion carries no node_modules of its own.
[ -d "$ROOT/extension/node_modules" ] || (cd "$ROOT/extension" && npm install --silent)
cd "$ROOT/companion"
# Through node, not `npm run`: npm runs scripts under cmd.exe on Windows, which
# cannot execute a POSIX `.bin/tsc` shim by relative path. CI's win32 job
# failed on exactly that.
node "$ROOT/extension/node_modules/typescript/bin/tsc" -p .

VERSION=$(node -p "require('./package.json').version")
MAIN=$(node -p "require('../extension/package.json').version")
if [ "$VERSION" != "$MAIN" ]; then
    echo "companion is $VERSION but the extension is $MAIN; release them together." >&2
    exit 1
fi
OUT="$ROOT/dist/cartograph-local-$VERSION.vsix"
mkdir -p "$ROOT/dist"
npx --yes @vscode/vsce@3 package --out "$OUT" --allow-missing-repository --skip-license
echo "$OUT"
