#!/usr/bin/env python3
"""Materialise the Cartograph skills pack where GitHub Copilot discovers it.

Copilot CLI and Copilot Chat both read project skills from `.github/skills/`,
and neither reads `skills/`. They also read `.claude/skills/` and
`.agents/skills/`; a copy there would only be a second copy of the same pack.

**Copies, not symlinks.** Git on Windows checks a symlink out as a text file
containing the target path, which a host then reads as a skill body and
silently ignores. A copy works on every machine the toolset has to reach, and
`--check` makes the duplication safe by failing loudly when it drifts.

    python scripts/install-skills.py --target /path/to/project
    python scripts/install-skills.py --target . --check
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import sys
from pathlib import Path

HOSTS = (".github/skills",)

#: The engine ships the pack as package data so `carto install` can write it on
#: a machine that never cloned this repo. It is a build input, not a discovery
#: path, but it drifts the same way — so it is kept in sync and checked here.
PACKAGE_DATA = "engine/cartograph/skills_data"


def iter_skills(source: Path):
    return sorted(p for p in source.glob("*/SKILL.md"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", type=Path, default=Path(__file__).resolve().parent.parent / "skills")
    ap.add_argument("--target", type=Path, required=True,
                    help="Project root to install into")
    ap.add_argument("--check", action="store_true",
                    help="Verify the copies match the source; change nothing")
    args = ap.parse_args()

    skills = iter_skills(args.source)
    if not skills:
        print(f"no skills under {args.source}/*/SKILL.md", file=sys.stderr)
        return 1

    drift: list[str] = []
    for host in (*HOSTS, PACKAGE_DATA):
        root = args.target / host
        for skill in skills:
            dest = root / skill.parent.name / "SKILL.md"
            if args.check:
                if not dest.exists():
                    drift.append(f"missing: {dest.relative_to(args.target)}")
                elif not filecmp.cmp(skill, dest, shallow=False):
                    drift.append(f"stale:   {dest.relative_to(args.target)}")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(skill, dest)
        # A skill deleted from source must not linger in a host directory,
        # where an agent would still find and follow it.
        if root.exists():
            known = {s.parent.name for s in skills}
            for stale in root.iterdir():
                if stale.is_dir() and stale.name not in known:
                    if args.check:
                        drift.append(f"orphan:  {stale.relative_to(args.target)}")
                    else:
                        shutil.rmtree(stale)

    if args.check:
        for line in drift:
            print(f"  FAIL  {line}")
        n = len(skills) * (len(HOSTS) + 1)
        print(f"\nskills install: {n - len(drift)}/{n} copies current")
        return 1 if drift else 0

    print(f"installed {len(skills)} skills into {', '.join(HOSTS)} "
          f"and the engine's package data, under {args.target}")
    for host in (*HOSTS, PACKAGE_DATA):
        print(f"  {host}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
