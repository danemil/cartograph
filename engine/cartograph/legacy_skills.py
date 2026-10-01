"""Skill copies that releases before 0.6.0 wrote outside ``.github/skills``.

Those releases wrote the pack to ``.claude/skills/<name>/`` and
``.agents/skills/<name>/`` as well. Both Copilot hosts read those directories,
so a stale copy keeps steering agents — to commands from an older pack, or to
``carto`` in a repository that no longer has it installed.

A directory is removed only when it is provably Cartograph's: it has a pack
name, holds a SKILL.md and nothing else, and that file's text is a version the
pack has shipped. The versions are recorded as digests in
``skills_shipped.json``, written by ``scripts/install-skills.py`` from git
history, so a person's own skill under the same name never matches.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from pathlib import Path

LEGACY_SKILL_DIRS = (".claude/skills", ".agents/skills")
SHIPPED_FILE = "skills_shipped.json"


def digest(data: bytes) -> str:
    # Releases wrote the file in text mode, so on Windows each line ended in
    # CRLF; the text is the same version either way.
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


@lru_cache(maxsize=1)
def shipped_digests() -> dict[str, frozenset[str]]:
    raw = resources.files("cartograph").joinpath(SHIPPED_FILE).read_text(encoding="utf-8")
    return {slug: frozenset(values) for slug, values in json.loads(raw).items()}


@dataclass
class Sweep:
    """Repo-relative directories, with a trailing slash."""

    removed: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    kept: list[tuple[str, str]] = field(default_factory=list)


def _symlinked(repo_root: Path, path: Path) -> bool:
    current = path
    while current != repo_root and current != current.parent:
        if current.is_symlink():
            return True
        current = current.parent
    return False


def _verdict(directory: Path, slug: str) -> str | None:
    """None when the directory is Cartograph's; otherwise why it is kept."""
    entries = sorted(p.name for p in directory.iterdir())
    if entries != ["SKILL.md"]:
        return "it holds files Cartograph never wrote"
    if (directory / "SKILL.md").is_symlink():
        return "its SKILL.md is a symlink"
    if digest((directory / "SKILL.md").read_bytes()) not in shipped_digests()[slug]:
        return "same name as a Cartograph skill, but not a version Cartograph shipped"
    return None


def sweep(repo_root: Path, *, dry_run: bool = False) -> Sweep:
    result = Sweep()
    for base_rel in LEGACY_SKILL_DIRS:
        base = repo_root / base_rel
        if not base.is_dir() or _symlinked(repo_root, base):
            continue
        removed_here = 0
        for slug in sorted(shipped_digests()):
            directory = base / slug
            rel = f"{base_rel}/{slug}/"
            if not directory.exists() and not directory.is_symlink():
                continue
            if directory.is_symlink() or not directory.is_dir():
                result.kept.append((rel, "not a plain directory"))
                continue
            try:
                reason = _verdict(directory, slug)
            except OSError as exc:
                reason = f"could not be read ({exc})"
            if reason:
                result.kept.append((rel, reason))
                continue
            if not dry_run:
                shutil.rmtree(directory)
            result.removed.append(rel)
            removed_here += 1
        # Only a directory this sweep emptied: one that was already empty, or
        # still holds anything, was not left by Cartograph alone.
        remaining = [
            p for p in base.iterdir() if f"{base_rel}/{p.name}/" not in result.removed
        ]
        if removed_here and not remaining:
            if not dry_run:
                base.rmdir()
            result.pruned.append(f"{base_rel}/")
    return result


def describe(result: Sweep, *, dry_run: bool = False) -> list[str]:
    """What install prints. The first words are stable: the extension logs
    lines that start with them."""
    lines: list[str] = []
    if result.removed:
        verb = "Would remove" if dry_run else "Removed"
        lines.append(
            f"{verb} skill copies an earlier Cartograph install left (Copilot reads "
            f"these too): {', '.join(result.removed + result.pruned)}"
        )
    for rel, reason in result.kept:
        lines.append(f"Left {rel}: {reason}")
    return lines
