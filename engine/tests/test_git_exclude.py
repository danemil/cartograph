"""``carto install`` keeps its files out of git locally, via ``info/exclude``.

Every test runs against a real repository made with ``git init``: the point is
what git itself reports, and a fake ``.git`` directory reports nothing.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cartograph import skills, uninstall
from cartograph.cli import _handle_init
from cartograph.git_exclude import BLOCK_BEGIN, BLOCK_END

SLUGS = sorted(skills.skill_documents())

#: What a default ``carto install`` (the extension's: no instruction file)
#: writes, as it appears in the managed block.
EXPECTED_BLOCK = (
    [BLOCK_BEGIN, "/.cartograph/"]
    + [f"/.github/skills/{slug}/" for slug in SLUGS]
    + ["/.github/hooks/cartograph.json", BLOCK_END]
)


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch, tmp_path):
    """Keep the developer's own git config out of the result.

    A global ``core.excludesFile`` could hide exactly the files these tests
    expect ``git status`` to be silent about, and the tests would pass for the
    wrong reason.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _install(repo: Path, **overrides) -> None:
    args = argparse.Namespace(
        repo=str(repo),
        dry_run=False,
        platform="copilot",
        yes=True,
        no_instructions=True,
        no_skills=False,
        no_hooks=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    _handle_init(args)


def _exclude_file(repo: Path) -> Path:
    out = _git(repo, "rev-parse", "--git-path", "info/exclude").strip()
    return (repo / out).resolve()


def _block(text: str) -> list[str]:
    lines = text.splitlines()
    start = lines.index(BLOCK_BEGIN)
    return lines[start : lines.index(BLOCK_END, start) + 1]


def test_install_adds_the_block_once_with_exactly_the_written_paths(tmp_path):
    repo = _repo(tmp_path / "repo")

    _install(repo)

    text = _exclude_file(repo).read_text(encoding="utf-8")
    assert _block(text) == EXPECTED_BLOCK
    assert text.count(BLOCK_BEGIN) == 1
    # git's own template comments, written by `git init`, survive.
    assert text.startswith("# git ls-files --others --exclude-from=.git/info/exclude")


def test_install_creates_info_exclude_when_missing(tmp_path):
    repo = _repo(tmp_path / "repo")
    exclude = _exclude_file(repo)
    exclude.unlink()
    exclude.parent.rmdir()

    _install(repo)

    assert _block(exclude.read_text(encoding="utf-8")) == EXPECTED_BLOCK


def test_reinstall_changes_nothing(tmp_path, capsys):
    repo = _repo(tmp_path / "repo")
    _install(repo)
    first = _exclude_file(repo).read_bytes()
    capsys.readouterr()

    _install(repo)

    assert _exclude_file(repo).read_bytes() == first
    assert "info/exclude already excludes" in capsys.readouterr().out


def test_instruction_file_is_excluded_only_when_install_writes_it(tmp_path):
    repo = _repo(tmp_path / "repo")

    _install(repo, no_instructions=False, no_skills=True, no_hooks=True)

    assert _block(_exclude_file(repo).read_text(encoding="utf-8")) == [
        BLOCK_BEGIN,
        "/.cartograph/",
        f"/{skills.INSTRUCTION_FILE}",
        BLOCK_END,
    ]


def test_worktree_writes_the_file_git_reads(tmp_path):
    """In a linked worktree ``.git`` is a file; ``--git-path`` finds the real one."""
    main = _repo(tmp_path / "main")
    worktree = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(worktree))
    assert (worktree / ".git").is_file()

    _install(worktree)

    assert _block((main / ".git" / "info" / "exclude").read_text(encoding="utf-8")) == (
        EXPECTED_BLOCK
    )
    assert _git(worktree, "status", "--porcelain") == ""


def test_tracked_paths_are_reported_and_not_excluded(tmp_path, capsys):
    """A team that committed the skills keeps seeing its changes to them."""
    repo = _repo(tmp_path / "repo")
    _install(repo)
    _git(repo, "add", "-f", ".github/skills")
    _git(repo, "commit", "-q", "-m", "commit the skills")
    capsys.readouterr()

    _install(repo)

    block = _block(_exclude_file(repo).read_text(encoding="utf-8"))
    assert block == [BLOCK_BEGIN, "/.cartograph/", "/.github/hooks/cartograph.json", BLOCK_END]
    out = capsys.readouterr().out
    for slug in SLUGS:
        assert f".github/skills/{slug}/" in out
    assert "tracked" in out


def test_gitignore_is_left_byte_for_byte(tmp_path):
    repo = _repo(tmp_path / "repo")
    original = b"node_modules/\r\ndist/"
    (repo / ".gitignore").write_bytes(original)

    _install(repo)

    assert (repo / ".gitignore").read_bytes() == original


def test_a_block_an_earlier_release_put_in_gitignore_stays(tmp_path):
    """Removing lines from a tracked file is what this design avoids."""
    repo = _repo(tmp_path / "repo")
    original = b"dist/\n# Added by cartograph\n.cartograph/\n"
    (repo / ".gitignore").write_bytes(original)

    _install(repo)

    assert (repo / ".gitignore").read_bytes() == original


def test_install_does_not_create_a_gitignore(tmp_path):
    repo = _repo(tmp_path / "repo")

    _install(repo)

    assert not (repo / ".gitignore").exists()


def test_uninstall_removes_only_the_block(tmp_path):
    repo = _repo(tmp_path / "repo")
    exclude = _exclude_file(repo)
    exclude.write_text("# mine\n*.log\n", encoding="utf-8")
    _install(repo)
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write("scratch/\n")

    report = uninstall.run(repo=repo, keep_data=True)

    assert report.errors == []
    assert exclude.read_text(encoding="utf-8") == "# mine\n*.log\nscratch/\n"


def test_uninstall_dry_run_leaves_the_block(tmp_path):
    repo = _repo(tmp_path / "repo")
    _install(repo)
    before = _exclude_file(repo).read_bytes()

    report = uninstall.run(repo=repo, keep_data=True, dry_run=True)

    assert _exclude_file(repo).read_bytes() == before
    assert any("info/exclude" in entry for entry in report.edited_paths)


def test_non_git_directory_is_skipped_with_a_note(tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()

    _install(plain)

    out = capsys.readouterr().out
    assert "Not a git repository" in out
    assert not (plain / ".git").exists()
    assert not (plain / ".gitignore").exists()


def test_end_to_end_git_status_shows_nothing_from_cartograph(tmp_path):
    """The real CLI, in a fresh repository, with graph data on disk."""
    repo = _repo(tmp_path / "repo")
    engine = Path(__file__).resolve().parents[1]

    subprocess.run(
        [sys.executable, "-m", "cartograph", "install", "--platform", "copilot", "-y",
         "--repo", str(repo)],
        cwd=repo,
        check=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(engine), "HOME": str(tmp_path / "home")},
    )
    # `carto build` writes here; its own inner .gitignore is removed so the
    # exclude block is what is being tested.
    (repo / ".cartograph").mkdir(exist_ok=True)
    (repo / ".cartograph" / ".gitignore").unlink(missing_ok=True)
    (repo / ".cartograph" / "graph.db").write_bytes(b"x")

    assert (repo / ".github" / "hooks" / "cartograph.json").is_file()
    assert (repo / skills.INSTRUCTION_FILE).is_file()
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""
