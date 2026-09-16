#!/usr/bin/env bash
# Verify everything Cartograph promises, against the shipped artifacts.
#
# Run from the repo root. Each stage shells out to the CLI rather than
# importing it, so the same suites can hold the TypeScript memory side to the
# same standard later.
set -uo pipefail
cd "$(dirname "$0")/.."

PY=engine/.venv/bin/python
fail=0

echo "== capability envelope =="
"$PY" contracts/capability-v1/check.py --manifest engine/contract-manifest.json | tail -1 || fail=1

echo
echo "== skills match the CLI =="
( cd engine && PYTHONPATH=. .venv/bin/python ../contracts/capability-v1/check_skills.py \
    --skills ../skills --command .venv/bin/python -m cartograph ) | tail -1 || fail=1

echo
echo "== skills are installed where hosts look =="
"$PY" scripts/install-skills.py --target . --check | tail -1 || fail=1

echo
if [ "$fail" -eq 0 ]; then echo "all green"; else echo "FAILURES above"; fi
exit "$fail"
