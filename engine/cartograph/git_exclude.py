"""Keep the files ``carto install`` writes out of ``git status``, locally.

The repository's ``info/exclude`` is used rather than ``.gitignore`` because it
is never committed: nothing a team shares is edited, and nothing Cartograph
writes gets committed by accident. The file is found with
``git rev-parse --git-path info/exclude`` because ``.git`` is a file in a
linked worktree and may be elsewhere entirely under ``GIT_DIR``; the path git
names is the one git reads.

A path git already tracks is left out of the block and reported. Exclusion has
no effect on a tracked file, so listing it would only suggest a protection that
does not exist.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

BLOCK_BEGIN = "# cartograph (managed)"
BLOCK_END = "# end cartograph"

#: The engine's state directory, excluded on every install whether or not a
#: build has created it yet.
DATA_DIR = ".cartograph/"


class NotAGitRepository(Exception):
    """``git`` is missing, or the directory is not inside a work tree."""


@dataclass
class ExcludeResult:
    path: Path
    excluded: list[str] = field(default_factory=list)
    tracked: list[str] = field(default_factory=list)
    changed: bool = False
    #: Set when a begin marker has no end marker. The file is then left alone:
    #: guessing where the block ends could delete the person's own lines.
    malformed: bool = False


def _git(repo_root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise NotAGitRepository(str(exc)) from exc
    if result.returncode != 0:
        raise NotAGitRepository(result.stderr.strip())
    return result.stdout


def exclude_file(repo_root: Path) -> Path:
    """The ``info/exclude`` git reads for this work tree."""
    if _git(repo_root, "rev-parse", "--is-inside-work-tree").strip() != "true":
        raise NotAGitRepository(f"{repo_root} is not inside a work tree")
    # Relative to repo_root in a plain checkout, absolute in a worktree.
    return (repo_root / _git(repo_root, "rev-parse", "--git-path", "info/exclude").strip())


def _pattern(prefix: str, relative: str) -> str:
    """An exclude line matching exactly this path from the top of the tree.

    Anchored with a leading ``/`` so ``.cartograph/`` in a subdirectory of the
    project is not matched too, and glob characters escaped because the prefix
    is whatever the directory happens to be called.
    """
    text = prefix + relative
    for char in "\\*?[":
        text = text.replace(char, "\\" + char)
    if text.endswith(" "):
        text = text[:-1] + "\\ "
    return "/" + text


def _is_tracked(repo_root: Path, relative: str) -> bool:
    return bool(_git(repo_root, "ls-files", "-z", "--", relative.rstrip("/")))


def _find_block(lines: list[str]) -> tuple[int, int] | None | bool:
    """``(begin, end)`` line indices, ``None`` if absent, ``False`` if unclosed."""
    for begin, line in enumerate(lines):
        if line.rstrip("\r\n") == BLOCK_BEGIN:
            for end in range(begin + 1, len(lines)):
                if lines[end].rstrip("\r\n") == BLOCK_END:
                    return begin, end
            return False
    return None


def read_exclude(path: Path) -> str:
    """The file's text with its line endings as they are on disk."""
    if not path.exists():
        return ""
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def without_block(text: str) -> str | None | bool:
    """``text`` with the block removed; ``None`` if absent, ``False`` if unclosed."""
    lines = text.splitlines(keepends=True)
    found = _find_block(lines)
    if not found:
        return found
    begin, end = found
    return "".join(lines[:begin] + lines[end + 1 :])


def exclude_locally(repo_root: Path, written: list[str]) -> ExcludeResult:
    """Write the managed block listing ``written`` (repo-relative) and ``.cartograph/``.

    The block is replaced whole on each install, so it always equals what this
    install wrote and re-running it changes nothing.

    Raises :class:`NotAGitRepository` when there is no git work tree.
    """
    path = exclude_file(repo_root)
    prefix = _git(repo_root, "rev-parse", "--show-prefix").strip()
    result = ExcludeResult(path=path)

    for relative in dict.fromkeys([DATA_DIR, *written]):
        if _is_tracked(repo_root, relative):
            result.tracked.append(relative)
        else:
            result.excluded.append(relative)

    existing = read_exclude(path)
    lines = existing.splitlines(keepends=True)
    found = _find_block(lines)
    if found is False:
        result.malformed = True
        return result

    block = "".join(
        line + "\n"
        for line in [BLOCK_BEGIN, *(_pattern(prefix, r) for r in result.excluded), BLOCK_END]
    ) if result.excluded else ""
    if found is None:
        separator = "\n" if existing and not existing.endswith("\n") else ""
        updated = existing + separator + block if block else existing
    else:
        begin, end = found
        # Replaced where it stands, so lines the person added after it stay
        # after it.
        updated = "".join(lines[:begin]) + block + "".join(lines[end + 1 :])

    if updated != existing:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
        result.changed = True
    return result
