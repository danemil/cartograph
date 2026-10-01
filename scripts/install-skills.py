#!/usr/bin/env python3
"""Materialise the Cartograph skills pack where GitHub Copilot discovers it.

Copilot CLI and Copilot Chat both read project skills from `.github/skills/`,
and neither reads `skills/`. They also read `.claude/skills/` and
`.agents/skills/`; a copy there would only be a second copy of the same pack.

**Copies, not symlinks.** Git on Windows checks a symlink out as a text file
containing the target path, which a host then reads as a skill body and
silently ignores. A copy works on every machine the toolset has to reach, and
`--check` makes the duplication safe by failing loudly when it drifts.

It also records every SKILL.md version the pack has shipped (from git history)
in `engine/cartograph/skills_shipped.json`, which is how `carto install`
recognises the copies releases before 0.6.0 left in `.claude/skills` and
`.agents/skills`.

    python scripts/install-skills.py --target /path/to/project
    python scripts/install-skills.py --target . --check
"""

from __future__ import annotations

import argparse
import filecmp
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

HOSTS = (".github/skills",)

#: The engine ships the pack as package data so `carto install` can write it on
#: a machine that never cloned this repo. It is a build input, not a discovery
#: path, but it drifts the same way — so it is kept in sync and checked here.
PACKAGE_DATA = "engine/cartograph/skills_data"

#: Digests of every SKILL.md text the pack has shipped, by skill. Releases
#: before 0.6.0 also wrote `.claude/skills` and `.agents/skills`; install
#: removes a copy there only when its text is one of these
#: (engine/cartograph/legacy_skills.py).
SHIPPED = "engine/cartograph/skills_shipped.json"


def _digest(data: bytes) -> str:
    # Must equal legacy_skills.digest: CRLF is the same text.
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def shipped_digests(repo: Path, source: Path) -> dict[str, set[str]]:
    """The recorded digests, plus every version git history and the source
    tree hold. Recorded ones are never dropped: a shallow clone has less
    history, and a version once shipped stays shipped."""
    found: dict[str, set[str]] = {}
    record = repo / SHIPPED
    if record.exists():
        for slug, values in json.loads(record.read_text(encoding="utf-8")).items():
            found.setdefault(slug, set()).update(values)
    rel = source.resolve().relative_to(repo.resolve()).as_posix()
    try:
        commits = subprocess.run(
            ["git", "rev-list", "--all", "--", rel], cwd=repo,
            check=True, capture_output=True, text=True,
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        commits = []
    blobs: set[tuple[str, str]] = set()
    for commit in commits:
        tree = subprocess.run(
            ["git", "ls-tree", "-r", commit, "--", f"{rel}/"], cwd=repo,
            check=True, capture_output=True, text=True,
        ).stdout
        for line in tree.splitlines():
            meta, path = line.split("\t", 1)
            parts = path[len(rel) + 1:].split("/")
            if len(parts) == 2 and parts[1] == "SKILL.md":
                blobs.add((parts[0], meta.split()[2]))
    for slug, blob in blobs:
        data = subprocess.run(["git", "cat-file", "blob", blob], cwd=repo,
                              check=True, capture_output=True).stdout
        found.setdefault(slug, set()).add(_digest(data))
    for skill in iter_skills(source):
        found.setdefault(skill.parent.name, set()).add(_digest(skill.read_bytes()))
    return found


def render_shipped(found: dict[str, set[str]]) -> str:
    return json.dumps({k: sorted(v) for k, v in sorted(found.items())}, indent=2) + "\n"


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

    repo = Path(__file__).resolve().parent.parent
    record = repo / SHIPPED
    wanted = render_shipped(shipped_digests(repo, args.source))
    if args.check:
        if not record.exists() or record.read_text(encoding="utf-8") != wanted:
            drift.append(f"stale:   {SHIPPED} (lacks a shipped version)")
    else:
        record.write_text(wanted, encoding="utf-8")

    if args.check:
        for line in drift:
            print(f"  FAIL  {line}")
        # The copies, and the record of shipped versions.
        n = len(skills) * (len(HOSTS) + 1) + 1
        print(f"\nskills install: {n - len(drift)}/{n} files current")
        return 1 if drift else 0

    print(f"installed {len(skills)} skills into {', '.join(HOSTS)} "
          f"and the engine's package data, under {args.target}")
    for host in (*HOSTS, PACKAGE_DATA):
        print(f"  {host}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
