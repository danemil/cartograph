"""Install and uninstall clear the skill copies releases before 0.6.0 left in
``.claude/skills`` and ``.agents/skills`` — and only those.

A copy is Cartograph's when its directory has a pack name and holds nothing
but a SKILL.md whose text is a version the pack has shipped. Anything else
with the same name is the person's, and stays.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from cartograph import legacy_skills, skills, uninstall
from cartograph.cli import _handle_init

REPO = Path(__file__).resolve().parents[2]
SLUGS = sorted(skills.skill_documents())
LEGACY = (".claude/skills", ".agents/skills")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


def _install(repo: Path, **overrides) -> None:
    args = argparse.Namespace(
        repo=str(repo), dry_run=False, platform="copilot", yes=True,
        no_instructions=True, no_skills=False, no_hooks=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    _handle_init(args)


def _place(repo: Path, base: str, slug: str, data: bytes) -> Path:
    d = repo / base / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_bytes(data)
    return d


def _current(slug: str) -> bytes:
    return skills.skill_documents()[slug].encode("utf-8")


def _old_version(slug: str) -> bytes:
    """A version from v0.5.0, the last release that wrote .claude/.agents."""
    return subprocess.run(
        ["git", "show", f"v0.5.0:skills/{slug}/SKILL.md"],
        cwd=REPO, check=True, capture_output=True,
    ).stdout


def test_install_removes_every_shipped_copy_and_the_dirs_it_emptied(tmp_path, capsys):
    repo = _repo(tmp_path / "r")
    for base in LEGACY:
        for slug in SLUGS:
            _place(repo, base, slug, _current(slug))

    _install(repo)

    for base in LEGACY:
        assert not (repo / base).exists()
        # The parent is not Cartograph's to judge: .claude holds settings.
        assert (repo / base).parent.is_dir()
    out = capsys.readouterr().out
    assert "Removed skill copies an earlier Cartograph install left" in out
    for base in LEGACY:
        assert f"{base}/explore-codebase/" in out
    assert (repo / ".github/skills/explore-codebase/SKILL.md").is_file()


def test_an_older_shipped_version_is_recognised(tmp_path):
    old = _old_version("explore-codebase")
    assert old != _current("explore-codebase"), "pick a skill that changed since v0.5.0"
    repo = _repo(tmp_path / "r")
    _place(repo, ".claude/skills", "explore-codebase", old)

    _install(repo)

    assert not (repo / ".claude/skills/explore-codebase").exists()


def test_windows_line_endings_are_the_same_text(tmp_path):
    repo = _repo(tmp_path / "r")
    _place(repo, ".agents/skills", "debug-issue",
           _current("debug-issue").replace(b"\n", b"\r\n"))

    _install(repo)

    assert not (repo / ".agents/skills/debug-issue").exists()


def test_same_name_with_the_users_content_stays_and_is_named(tmp_path, capsys):
    repo = _repo(tmp_path / "r")
    mine = _current("explore-codebase") + b"\nMy team's extra rule.\n"
    d = _place(repo, ".claude/skills", "explore-codebase", mine)

    _install(repo)

    assert (d / "SKILL.md").read_bytes() == mine
    out = capsys.readouterr().out
    assert ".claude/skills/explore-codebase/" in out
    assert "not a version Cartograph shipped" in out


def test_extra_files_beside_a_shipped_skill_keep_it(tmp_path, capsys):
    repo = _repo(tmp_path / "r")
    d = _place(repo, ".claude/skills", "review-changes", _current("review-changes"))
    (d / "notes.md").write_text("mine\n", encoding="utf-8")

    _install(repo)

    assert (d / "SKILL.md").is_file() and (d / "notes.md").is_file()
    assert "files Cartograph never wrote" in capsys.readouterr().out


def test_other_skills_and_their_directory_are_untouched(tmp_path):
    repo = _repo(tmp_path / "r")
    _place(repo, ".claude/skills", "explore-codebase", _current("explore-codebase"))
    theirs = _place(repo, ".claude/skills", "deploy-app", b"---\nname: deploy-app\n---\n")

    _install(repo)

    assert not (repo / ".claude/skills/explore-codebase").exists()
    assert (theirs / "SKILL.md").is_file()


def test_an_empty_directory_cartograph_did_not_empty_stays(tmp_path):
    repo = _repo(tmp_path / "r")
    (repo / ".agents/skills").mkdir(parents=True)

    _install(repo)

    assert (repo / ".agents/skills").is_dir()


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlinked_skills_directory_is_never_followed(tmp_path):
    repo = _repo(tmp_path / "r")
    elsewhere = tmp_path / "elsewhere"
    _place(elsewhere, "skills", "build-graph", _current("build-graph"))
    (repo / ".claude").mkdir()
    (repo / ".claude/skills").symlink_to(elsewhere / "skills")

    _install(repo)

    assert (elsewhere / "skills/build-graph/SKILL.md").is_file()


def test_dry_run_install_names_them_and_removes_nothing(tmp_path, capsys):
    repo = _repo(tmp_path / "r")
    d = _place(repo, ".claude/skills", "build-graph", _current("build-graph"))

    _install(repo, dry_run=True)

    assert (d / "SKILL.md").is_file()
    assert ".claude/skills/build-graph/" in capsys.readouterr().out


def test_install_without_skills_leaves_them(tmp_path):
    repo = _repo(tmp_path / "r")
    d = _place(repo, ".claude/skills", "build-graph", _current("build-graph"))

    _install(repo, no_skills=True)

    assert (d / "SKILL.md").is_file()


def test_uninstall_removes_shipped_copies_and_reports_the_users(tmp_path):
    repo = _repo(tmp_path / "r")
    _place(repo, ".agents/skills", "recall-context", _current("recall-context"))
    mine = _place(repo, ".claude/skills", "refactor-safely", b"my own refactor skill\n")

    preview = uninstall.run(repo=repo, dry_run=True)
    assert (repo / ".agents/skills/recall-context/SKILL.md").is_file()
    assert any("recall-context" in p and "would" in p for p in preview.removed_paths)

    report = uninstall.run(repo=repo, dry_run=False)

    assert not (repo / ".agents/skills").exists()
    assert (mine / "SKILL.md").is_file()
    assert any("refactor-safely" in p for p in report.skipped_paths)
    assert any(".agents/skills" in p for p in report.removed_paths)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def test_the_record_holds_every_version_in_history_and_the_current_pack():
    recorded = json.loads(
        (REPO / "engine/cartograph" / legacy_skills.SHIPPED_FILE).read_text(encoding="utf-8")
    )
    assert sorted(recorded) == SLUGS
    for slug in SLUGS:
        assert _digest((REPO / "skills" / slug / "SKILL.md").read_bytes()) in recorded[slug]
        assert _digest(_current(slug)) in recorded[slug]
    assert _digest(_old_version("explore-codebase")) in recorded["explore-codebase"]
