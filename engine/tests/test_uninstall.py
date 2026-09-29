"""Destructive-regression tests for the safe uninstall workflow.

Every test uses a fake home and repository.  The real user configuration must
never be reachable from this suite.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from cartograph import skills, uninstall

INSTRUCTIONS = Path(skills.INSTRUCTION_FILE)
HOOK_FILE = Path(".github") / "hooks" / "cartograph.json"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, value: object) -> None:
    _write(path, json.dumps(value, indent=2) + "\n")


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git" / "hooks").mkdir(parents=True)
    return repo


def _legacy_copilot_section() -> str:
    """The longest recorded Copilot block from an earlier release."""
    return next(
        block
        for block in skills.LEGACY_INSTRUCTION_SECTIONS
        if block.startswith("---\n")
    )


def test_hook_file_is_removed_and_team_hooks_are_kept(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    ours = fake_repo / HOOK_FILE
    theirs = ours.parent / "team.json"
    skills.install_copilot_hooks(fake_repo)
    _write_json(theirs, {"hooks": {}})

    report = uninstall.run(repo=fake_repo, keep_data=True)

    assert report.errors == []
    assert not ours.exists()
    assert theirs.exists()


def test_skill_directory_keeps_user_files_and_unrelated_skills(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    root = fake_repo / skills.HOST_SKILL_DIR
    generated_slug = next(iter(skills._SKILLS)).removesuffix(".md")
    _write(root / generated_slug / "SKILL.md", "generated\n")
    _write(root / generated_slug / "notes.txt", "keep\n")
    _write(root / "user-skill" / "SKILL.md", "keep\n")

    uninstall.run(repo=fake_repo, keep_data=True)

    assert not (root / generated_slug / "SKILL.md").exists()
    assert (root / generated_slug / "notes.txt").exists()
    assert (root / "user-skill" / "SKILL.md").exists()


def test_installed_skills_are_all_removed(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    root = skills.install_host_skills(fake_repo)

    uninstall.run(repo=fake_repo, keep_data=True)

    assert list(root.iterdir()) == []


def test_uninstall_cleans_current_and_legacy_copilot_instruction_paths(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    """Both Copilot paths lose only the generated instruction section."""
    paths = (
        fake_repo / INSTRUCTIONS,
        fake_repo / skills.LEGACY_INSTRUCTION_FILE,
    )
    for path in paths:
        _write(path, "# User notes\n\n" + skills._COPILOT_SECTION)

    report = uninstall.run(repo=fake_repo, keep_data=True)

    assert report.errors == []
    for path in paths:
        assert path.read_text(encoding="utf-8") == "# User notes\n"


def test_uninstall_removes_a_section_written_by_an_older_release(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    """Removal used to match only the running version's text, so a block from
    any earlier release survived an explicit uninstall (#314)."""
    path = fake_repo / INSTRUCTIONS
    _write(path, "user instructions\n\n" + _legacy_copilot_section())

    report = uninstall.run(repo=fake_repo, keep_data=True)

    assert report.errors == []
    assert path.read_text(encoding="utf-8") == "user instructions\n"
    assert report.skipped_paths == []


def test_uninstall_removes_the_current_section_including_its_end_marker(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    path = fake_repo / INSTRUCTIONS
    _write(path, "user instructions\n\n" + skills._COPILOT_SECTION)

    uninstall.run(repo=fake_repo, keep_data=True)

    remaining = path.read_text(encoding="utf-8")
    assert remaining == "user instructions\n"
    assert skills._SECTION_END_MARKER not in remaining


def test_a_generated_only_instruction_file_is_deleted(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    skills.inject_instruction_files(fake_repo)

    uninstall.run(repo=fake_repo, keep_data=True)

    assert not (fake_repo / INSTRUCTIONS).exists()


def test_uninstall_leaves_a_hand_edited_section_alone_and_reports_it(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    path = fake_repo / INSTRUCTIONS
    content = "user prefix\n\n" + _legacy_copilot_section().replace(
        "### ", "### (our notes) ", 1
    )
    _write(path, content)

    report = uninstall.run(repo=fake_repo, keep_data=True)

    assert path.read_text(encoding="utf-8") == content
    assert any(str(path) in item and "left unchanged" in item for item in report.skipped_paths)


def test_uninstall_keeps_user_content_on_both_sides_of_the_block(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    head = "# House rules\n\nNever force push.\n\n"
    tail = "\n## Deploy notes\n\nRun the migration first.\n"
    path = fake_repo / INSTRUCTIONS
    _write(path, head + _legacy_copilot_section() + tail)

    uninstall.run(repo=fake_repo, keep_data=True)

    remaining = path.read_text(encoding="utf-8")
    assert remaining == (
        "# House rules\n\nNever force push.\n\n## Deploy notes\n\nRun the migration first.\n"
    )
    assert skills._SECTION_MARKER not in remaining
    # One blank line where the block was, not a pileup left by the removal.
    assert "\n\n\n" not in remaining


def test_uninstall_clears_every_duplicate_stale_block(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    """Repeat installs used to stack blocks (#558); removal must clear them all."""
    older = [
        block
        for block in skills.LEGACY_INSTRUCTION_SECTIONS
        if block.startswith("---\n")
    ]
    path = fake_repo / INSTRUCTIONS
    _write(path, "user instructions\n\n" + older[0] + "\n" + older[1] + "\n" + older[0])

    report = uninstall.run(repo=fake_repo, keep_data=True)

    assert report.errors == []
    assert path.read_text(encoding="utf-8") == "user instructions\n"


def test_install_then_uninstall_restores_the_file_byte_for_byte(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    original = "# House rules\n\nNever force push.\n"
    path = fake_repo / INSTRUCTIONS
    _write(path, original)

    skills.inject_instruction_files(fake_repo)
    assert path.read_text(encoding="utf-8") != original

    uninstall.run(repo=fake_repo, keep_data=True)

    assert path.read_text(encoding="utf-8") == original


def test_modified_instruction_section_is_not_guessed_or_truncated(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    path = fake_repo / INSTRUCTIONS
    content = (
        "user prefix\n"
        f"{skills._SECTION_MARKER}\n"
        "user modified this formerly generated section\n"
        "user suffix that must not be truncated\n"
    )
    _write(path, content)

    report = uninstall.run(repo=fake_repo, keep_data=True)

    assert path.read_text(encoding="utf-8") == content
    assert any(str(path) in item and "left unchanged" in item for item in report.skipped_paths)


def test_only_installer_owned_gitignore_block_is_removed(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    gitignore = fake_repo / ".gitignore"
    _write(
        gitignore,
        "dist/\n# Added by cartograph\n.cartograph/\ncoverage/\n",
    )

    uninstall.run(repo=fake_repo, keep_data=True)

    assert gitignore.read_text(encoding="utf-8") == "dist/\ncoverage/\n"

    # An unmarked entry may have been written by the user before install.
    _write(gitignore, "dist/\n.cartograph/\n")
    uninstall.run(repo=fake_repo, keep_data=True)
    assert gitignore.read_text(encoding="utf-8") == "dist/\n.cartograph/\n"


def test_dry_run_is_meaningful_and_byte_for_byte_read_only(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    instructions = fake_repo / INSTRUCTIONS
    _write(instructions, "user instructions\n\n" + skills._COPILOT_SECTION)
    hook_file = skills.install_copilot_hooks(fake_repo)
    data = fake_repo / ".cartograph" / "graph.db"
    data.parent.mkdir()
    data.write_bytes(b"graph")
    before = {
        path: path.read_bytes()
        for path in (instructions, hook_file, data)
    }

    report = uninstall.run(repo=fake_repo, dry_run=True)

    assert report.total_actions >= 3
    assert any(str(instructions) in action for action in report.edited_paths)
    assert any(str(hook_file) in action for action in report.removed_paths)
    assert any(str(data.parent) in action for action in report.removed_paths)
    for path, content in before.items():
        assert path.read_bytes() == content


@pytest.mark.parametrize("failure_point", ("fsync", "replace"))
def test_failed_atomic_edit_preserves_original_bytes(
    failure_point: str,
    fake_repo: Path,
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = fake_repo / INSTRUCTIONS
    _write(path, "user instructions\n\n" + skills._COPILOT_SECTION)
    original = path.read_bytes()

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError(f"simulated {failure_point} failure")

    monkeypatch.setattr(uninstall.os, failure_point, fail)

    report = uninstall.run(
        repo=fake_repo,
        keep_data=True,
        keep_user_configs=True,
    )

    assert path.read_bytes() == original
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))
    assert any(
        str(path) in error and f"simulated {failure_point} failure" in error
        for error in report.errors
    )


def test_atomic_edit_preserves_file_mode(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    path = fake_repo / INSTRUCTIONS
    _write(path, "user instructions\n\n" + skills._COPILOT_SECTION)
    path.chmod(0o640)

    report = uninstall.run(
        repo=fake_repo,
        keep_data=True,
        keep_user_configs=True,
    )

    assert report.errors == []
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert path.read_text(encoding="utf-8") == "user instructions\n"


def test_non_repository_directory_is_refused_without_deleting_data(
    tmp_path: Path,
    fake_home: Path,
) -> None:
    ordinary_directory = tmp_path / "ordinary-directory"
    data = ordinary_directory / ".cartograph" / "unrelated.txt"
    instructions = ordinary_directory / INSTRUCTIONS
    _write(data, "not owned by CRG")
    _write(instructions, skills._COPILOT_SECTION)

    report = uninstall.run(
        repo=ordinary_directory,
        keep_user_configs=True,
    )

    assert data.read_text(encoding="utf-8") == "not owned by CRG"
    assert instructions.read_text(encoding="utf-8") == skills._COPILOT_SECTION
    assert report.total_actions == 0
    assert any(
        str(ordinary_directory) in item and "Git or SVN repository" in item
        for item in report.skipped_paths
    )


def test_repository_subdirectory_normalises_to_vcs_root(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    nested = fake_repo / "src" / "package"
    nested.mkdir(parents=True)
    data = fake_repo / ".cartograph" / "graph.db"
    data.parent.mkdir()
    data.write_bytes(b"graph")

    report = uninstall.run(
        repo=nested,
        keep_user_configs=True,
    )

    assert report.errors == []
    assert not data.parent.exists()


def test_symlinked_paths_are_skipped(
    fake_repo: Path,
    fake_home: Path,
    tmp_path: Path,
) -> None:
    outside_data = tmp_path / "outside-data"
    outside_data.mkdir()
    _write(outside_data / "keep.txt", "keep")
    os.symlink(outside_data, fake_repo / ".cartograph", target_is_directory=True)

    outside_github = tmp_path / "outside-github"
    _write(outside_github / "hooks" / "cartograph.json", "{}\n")
    os.symlink(outside_github, fake_repo / ".github", target_is_directory=True)

    report = uninstall.run(repo=fake_repo)

    assert (outside_data / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert (fake_repo / ".cartograph").is_symlink()
    assert (outside_github / "hooks" / "cartograph.json").exists()
    assert any("boundary" in item or "symlink" in item for item in report.skipped_paths)


def test_partial_filesystem_failure_is_reported_and_does_not_stop_cleanup(
    fake_repo: Path,
    fake_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocked = fake_repo / ".code-review-graph.db"
    blocked.write_bytes(b"db")
    removable = fake_repo / ".code-review-graph.db-wal"
    removable.write_bytes(b"wal")
    original_unlink = Path.unlink

    def fail_one(path: Path, *args: object, **kwargs: object) -> None:
        if path == blocked:
            raise PermissionError("simulated denial")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_one)

    report = uninstall.run(repo=fake_repo)

    assert blocked.exists()
    assert not removable.exists()
    assert any(str(blocked) in error and "simulated denial" in error for error in report.errors)


def test_second_run_is_idempotent(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    path = fake_repo / INSTRUCTIONS
    _write(path, "user instructions\n\n" + skills._COPILOT_SECTION)

    first = uninstall.run(repo=fake_repo, keep_data=True)
    second = uninstall.run(repo=fake_repo, keep_data=True)

    assert first.total_actions == 1
    assert second.total_actions == 0
    assert second.errors == []
    assert path.read_text(encoding="utf-8") == "user instructions\n"


def test_keep_flags_preserve_data_and_user_state(
    fake_repo: Path,
    fake_home: Path,
) -> None:
    repo_data = fake_repo / ".cartograph"
    repo_data.mkdir()
    (repo_data / "graph.db").write_bytes(b"db")
    legacy = fake_repo / ".code-review-graph.db"
    legacy.write_bytes(b"db")
    user_data = fake_home / ".cartograph"
    user_data.mkdir()
    (user_data / "registry.json").write_text("{}", encoding="utf-8")

    uninstall.run(
        repo=fake_repo,
        keep_data=True,
        keep_user_configs=True,
    )

    assert repo_data.exists()
    assert legacy.exists()
    assert user_data.exists()


def test_all_repos_reads_registry_before_removing_user_data(
    fake_repo: Path,
    fake_home: Path,
    tmp_path: Path,
) -> None:
    registered = tmp_path / "registered"
    (registered / ".git").mkdir(parents=True)
    registered_hooks = skills.install_copilot_hooks(registered)
    external_data = tmp_path / "external-data"
    external_data.mkdir()
    (external_data / "graph.db").write_bytes(b"keep")
    registry_dir = fake_home / ".cartograph"
    registry_dir.mkdir()
    _write_json(
        registry_dir / "registry.json",
        {"repos": [{"path": str(registered), "data_dir": str(external_data)}]},
    )

    report = uninstall.run(repo=fake_repo, all_repos=True)

    assert not registered_hooks.exists()
    assert not registry_dir.exists()
    assert (external_data / "graph.db").read_bytes() == b"keep"
    assert any(str(external_data) in item and "retained" in item for item in report.skipped_paths)


def test_cli_dry_run_and_confirmation_are_safe(
    fake_repo: Path,
    fake_home: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cartograph import cli

    hook_file = skills.install_copilot_hooks(fake_repo)

    with patch.object(
        sys,
        "argv",
        ["cartograph", "uninstall", "--repo", str(fake_repo), "--dry-run"],
    ):
        cli.main()
    assert "dry-run" in capsys.readouterr().out.lower()
    assert hook_file.exists()

    with (
        patch.object(
            sys,
            "argv",
            ["cartograph", "uninstall", "--repo", str(fake_repo)],
        ),
        patch.object(cli, "_confirm_yes_no", return_value=False),
    ):
        cli.main()
    assert "aborted" in capsys.readouterr().out.lower()
    assert hook_file.exists()
