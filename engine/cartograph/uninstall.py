"""Safely reverse artifacts created by :mod:`cartograph.skills`.

The original implementation was contributed by Stephen Cheng in PR #491.
This replacement keeps that command/report design while treating every shared
file as user-owned data that may only be edited surgically.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import skills

_GITIGNORE_BANNER = "# Added by cartograph"


@dataclass
class UninstallReport:
    """Structured record of completed or planned uninstall actions."""

    removed_paths: list[str] = field(default_factory=list)
    edited_paths: list[str] = field(default_factory=list)
    skipped_paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_actions(self) -> int:
        return len(self.removed_paths) + len(self.edited_paths)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _is_lexical_child(path: Path, boundary: Path) -> bool:
    candidate = _absolute(path)
    root = _absolute(boundary)
    if candidate == root:
        return False
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_path(
    path: Path,
    boundary: Path,
    report: UninstallReport,
    *,
    describe_skip: bool = True,
) -> bool:
    """Require lexical and resolved containment, with no symlink traversal."""
    candidate = _absolute(path)
    root = _absolute(boundary)
    if not _is_lexical_child(candidate, root):
        if describe_skip:
            report.skipped_paths.append(f"{candidate} (outside allowed boundary {root})")
        return False

    try:
        resolved_root = root.resolve(strict=False)
        resolved_candidate = candidate.resolve(strict=False)
        resolved_candidate.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError):
        if describe_skip:
            report.skipped_paths.append(f"{candidate} (resolved outside allowed boundary {root})")
        return False

    current = candidate
    while current != root:
        try:
            if current.is_symlink():
                if describe_skip:
                    report.skipped_paths.append(f"{candidate} (symlink path is not removed)")
                return False
        except OSError as exc:
            if describe_skip:
                report.skipped_paths.append(f"{candidate} (cannot inspect path: {exc})")
            return False
        current = current.parent
    return True


def _record_edit(report: UninstallReport, path: Path, detail: str, dry_run: bool) -> None:
    verb = f"would {detail}" if dry_run else detail
    report.edited_paths.append(f"{path} ({verb})")


def _record_remove(report: UninstallReport, path: Path, detail: str, dry_run: bool) -> None:
    verb = f"would {detail}" if dry_run else detail
    report.removed_paths.append(f"{path} ({verb})")


def _read_text(path: Path, report: UninstallReport) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        report.errors.append(f"{path}: read failed ({exc})")
        return None


def _write_text(
    path: Path,
    text: str,
    report: UninstallReport,
    *,
    detail: str,
    dry_run: bool,
) -> None:
    if dry_run:
        _record_edit(report, path, detail, True)
        return
    temporary: Path | None = None
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        temporary = None
    except (OSError, UnicodeError) as exc:
        report.errors.append(f"{path}: write failed ({exc})")
        return
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                report.errors.append(f"{temporary}: temporary cleanup failed ({exc})")
    _record_edit(report, path, detail, False)


def _remove_file(
    path: Path,
    boundary: Path,
    report: UninstallReport,
    *,
    dry_run: bool,
    detail: str = "removed owned file",
) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if not _safe_path(path, boundary, report):
        return
    if dry_run:
        _record_remove(report, path, detail, True)
        return
    try:
        path.unlink()
    except OSError as exc:
        report.errors.append(f"{path}: remove failed ({exc})")
        return
    _record_remove(report, path, detail, False)


def _remove_tree(
    path: Path,
    boundary: Path,
    report: UninstallReport,
    *,
    dry_run: bool,
) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if not _safe_path(path, boundary, report):
        return
    if not path.is_dir():
        report.skipped_paths.append(f"{path} (expected an owned directory; left unchanged)")
        return
    if dry_run:
        _record_remove(report, path, "remove owned directory", True)
        return
    try:
        shutil.rmtree(path)
    except OSError as exc:
        report.errors.append(f"{path}: remove failed ({exc})")
        return
    _record_remove(report, path, "removed owned directory", False)


def _prune_empty_directory(path: Path, boundary: Path) -> None:
    if not path.is_dir() or path.is_symlink() or not _is_lexical_child(path, boundary):
        return
    try:
        path.rmdir()
    except OSError:
        return


def _remove_skill_file(
    path: Path,
    boundary: Path,
    report: UninstallReport,
    *,
    dry_run: bool,
) -> None:
    existed = path.exists()
    _remove_file(path, boundary, report, dry_run=dry_run, detail="remove generated skill")
    if existed and not dry_run and not path.exists():
        _prune_empty_directory(path.parent, boundary)


def _join_without_instruction(before: str, after: str) -> str:
    """Close the gap a removed instruction block leaves behind.

    The text on either side is kept. Only the whitespace at the seam is
    normalised, so removing a block from the middle of a file does not leave a
    pile of blank lines where it used to be.
    """
    if not after.strip():
        # The block ran to the end, which is where install appends it.
        return before.rstrip() + ("\n" if before.strip() else "")
    if not before.strip():
        return after.lstrip("\n")
    return before.rstrip("\n") + "\n\n" + after.lstrip("\n")


def _remove_instruction(
    path: Path,
    boundary: Path,
    report: UninstallReport,
    *,
    dry_run: bool,
) -> None:
    """Strip every generated instruction block from a file.

    Only text that exactly equals a block this project generated is removed, so
    a section someone edited by hand survives and is reported instead. Blocks
    written before the closing marker existed have no end boundary, which is why
    nothing here searches for one: their full recorded text is the boundary.
    Matching every known variant means a block from any past release comes out,
    not just one written by the running version (#314).
    """
    if not path.exists() or not _safe_path(path, boundary, report):
        return
    raw = _read_text(path, report)
    if raw is None:
        return

    rewritten = raw
    # Longest first, so removing a short variant cannot strand the tail of a
    # longer one that contains it. The loop also clears duplicate blocks that
    # older releases stacked up (#558).
    for known in skills._known_instruction_sections():
        while (index := rewritten.find(known)) >= 0:
            rewritten = _join_without_instruction(
                rewritten[:index], rewritten[index + len(known) :]
            )

    if rewritten == raw:
        if skills._SECTION_MARKER in raw:
            report.skipped_paths.append(
                f"{path} (marked instruction section differs from a known installed section; "
                "left unchanged)"
            )
        return

    if skills._SECTION_MARKER in rewritten:
        # One block was generated and another was edited. Remove what this
        # project owns and name the file so the user can deal with the rest.
        report.skipped_paths.append(
            f"{path} (a further marked instruction section differs from a known "
            "installed section; left unchanged)"
        )

    if rewritten:
        _write_text(
            path,
            rewritten,
            report,
            detail="removed carto instruction section",
            dry_run=dry_run,
        )
    else:
        _remove_file(
            path,
            boundary,
            report,
            dry_run=dry_run,
            detail="remove generated instruction file",
        )


def _remove_gitignore(
    repo_root: Path,
    report: UninstallReport,
    *,
    dry_run: bool,
) -> None:
    path = repo_root / ".gitignore"
    if not path.exists() or not _safe_path(path, repo_root, report):
        return
    raw = _read_text(path, report)
    if raw is None:
        return
    lines = raw.splitlines(keepends=True)
    rewritten: list[str] = []
    changed = False
    index = 0
    while index < len(lines):
        if (
            lines[index].strip() == _GITIGNORE_BANNER
            and index + 1 < len(lines)
            and lines[index + 1].strip() in {".cartograph", ".cartograph/"}
        ):
            changed = True
            index += 2
            continue
        rewritten.append(lines[index])
        index += 1
    if not changed:
        return
    new_text = "".join(rewritten)
    if new_text.strip():
        _write_text(
            path,
            new_text,
            report,
            detail="removed installer-owned ignore block",
            dry_run=dry_run,
        )
    else:
        _remove_file(
            path,
            repo_root,
            report,
            dry_run=dry_run,
            detail="remove generated .gitignore",
        )


def _remove_exclude_block(
    repo_root: Path,
    report: UninstallReport,
    *,
    dry_run: bool,
) -> None:
    """Take the block install added out of ``info/exclude``, and nothing else.

    The file is git's own and usually outside ``repo_root`` in a worktree, so
    it is edited in place rather than removed even when the block was all it
    held.
    """
    from .git_exclude import NotAGitRepository, exclude_file, read_exclude, without_block

    try:
        path = exclude_file(repo_root)
    except NotAGitRepository:
        return
    try:
        raw = read_exclude(path)
    except (OSError, UnicodeError) as exc:
        report.errors.append(f"{path}: read failed ({exc})")
        return
    rewritten = without_block(raw)
    if rewritten is None:
        return
    if rewritten is False:
        report.skipped_paths.append(
            f"{path} (cartograph block has no end marker; left unchanged)"
        )
        return
    _write_text(
        path,
        rewritten,
        report,
        detail="removed cartograph exclude block",
        dry_run=dry_run,
    )


def _generated_skill_slugs() -> list[str]:
    return [filename.rsplit(".", 1)[0] for filename in skills._SKILLS]


def _process_repo(
    repo_root: Path,
    report: UninstallReport,
    *,
    keep_data: bool,
    dry_run: bool,
) -> None:
    # Pre-rename state is listed alongside current state: uninstall that leaves
    # `.code-review-graph/` behind has not uninstalled anything the user can see.
    from .incremental import LEGACY_DATA_DIR, LEGACY_DB_FILE

    data_paths = [
        (repo_root / ".cartograph", "tree"),
        (repo_root / LEGACY_DATA_DIR, "tree"),
        (repo_root / LEGACY_DB_FILE, "file"),
        (repo_root / f"{LEGACY_DB_FILE}-wal", "file"),
        (repo_root / f"{LEGACY_DB_FILE}-shm", "file"),
    ]
    for path, kind in data_paths:
        if keep_data:
            if path.exists() or path.is_symlink():
                report.skipped_paths.append(f"{path} (kept by --keep-data)")
            continue
        if kind == "tree":
            _remove_tree(path, repo_root, report, dry_run=dry_run)
        else:
            _remove_file(path, repo_root, report, dry_run=dry_run)

    # Cartograph's own file, written whole by install; a team's hooks live in
    # other files in the same directory and are never touched.
    _remove_file(
        repo_root / ".github" / "hooks" / "cartograph.json",
        repo_root,
        report,
        dry_run=dry_run,
        detail="remove generated hook file",
    )

    for slug in _generated_skill_slugs():
        _remove_skill_file(
            repo_root / skills.HOST_SKILL_DIR / slug / "SKILL.md",
            repo_root,
            report,
            dry_run=dry_run,
        )

    # Copies releases before 0.6.0 wrote beside .github/skills; the same
    # recognition install uses, so a person's own skill survives both.
    from .legacy_skills import sweep

    legacy = sweep(repo_root, dry_run=dry_run)
    for rel in legacy.removed:
        _record_remove(report, repo_root / rel, "remove stale skill copy", dry_run)
    for rel in legacy.pruned:
        _record_remove(report, repo_root / rel, "remove emptied skills directory", dry_run)
    for rel, reason in legacy.kept:
        report.skipped_paths.append(f"{repo_root / rel} ({reason}; left unchanged)")

    # Every variant is matched per file, so which section a given path was
    # written with no longer has to be worked out here.
    for relative in (skills.INSTRUCTION_FILE, skills.LEGACY_INSTRUCTION_FILE):
        _remove_instruction(
            repo_root / relative,
            repo_root,
            report,
            dry_run=dry_run,
        )

    _remove_gitignore(repo_root, report, dry_run=dry_run)
    _remove_exclude_block(repo_root, report, dry_run=dry_run)


def _process_user(
    home: Path,
    report: UninstallReport,
    *,
    keep_data: bool,
    dry_run: bool,
) -> None:
    user_data = home / ".cartograph"
    if keep_data:
        if user_data.exists() or user_data.is_symlink():
            report.skipped_paths.append(f"{user_data} (kept by --keep-data)")
    else:
        _remove_tree(user_data, home, report, dry_run=dry_run)


def _registry_repo_paths(home: Path, report: UninstallReport) -> list[Path]:
    path = home / ".cartograph" / "registry.json"
    if not path.exists() or not _safe_path(path, home, report):
        return []
    raw = _read_text(path, report)
    if raw is None:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as exc:
        report.skipped_paths.append(f"{path} (registry parse failed: {exc})")
        return []
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, list):
        report.skipped_paths.append(f"{path} (registry has no valid repos array)")
        return []
    paths: list[Path] = []
    for entry in repos:
        value = entry.get("path") if isinstance(entry, dict) else None
        if isinstance(value, str) and value:
            paths.append(Path(value).expanduser())
        data_dir = entry.get("data_dir") if isinstance(entry, dict) else None
        if isinstance(data_dir, str) and data_dir:
            report.skipped_paths.append(
                f"{Path(data_dir).expanduser()} (external data directory retained for safety)"
            )
    return paths


def _normalise_repo(path: Path, home: Path, report: UninstallReport) -> Path | None:
    lexical = _absolute(path)
    try:
        resolved = lexical.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        report.skipped_paths.append(f"{lexical} (cannot resolve repository: {exc})")
        return None
    filesystem_root = Path(resolved.anchor)
    if resolved in {filesystem_root, home.resolve(strict=False)}:
        report.skipped_paths.append(f"{resolved} (refusing unsafe repository boundary)")
        return None
    if not resolved.is_dir():
        report.skipped_paths.append(f"{resolved} (repository directory is missing)")
        return None

    from .incremental import find_repo_root

    repository_root = find_repo_root(resolved)
    if repository_root is None:
        report.skipped_paths.append(f"{resolved} (not inside a Git or SVN repository)")
        return None
    try:
        repository_root = repository_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        report.skipped_paths.append(f"{resolved} (cannot resolve repository root: {exc})")
        return None
    if repository_root in {Path(repository_root.anchor), home.resolve(strict=False)}:
        report.skipped_paths.append(
            f"{repository_root} (refusing unsafe repository boundary)"
        )
        return None
    return repository_root


def run(
    *,
    repo: Path | None = None,
    all_repos: bool = False,
    keep_data: bool = False,
    keep_user_configs: bool = False,
    dry_run: bool = False,
) -> UninstallReport:
    """Uninstall CRG artifacts and return a precise action report."""
    report = UninstallReport()
    home = _absolute(Path.home())
    requested = [repo if repo is not None else Path.cwd()]
    if all_repos:
        requested.extend(_registry_repo_paths(home, report))

    roots: list[Path] = []
    for candidate in requested:
        normalised = _normalise_repo(Path(candidate), home, report)
        if normalised is not None and normalised not in roots:
            roots.append(normalised)

    for root in roots:
        _process_repo(root, report, keep_data=keep_data, dry_run=dry_run)

    if not keep_user_configs:
        _process_user(home, report, keep_data=keep_data, dry_run=dry_run)
    return report
