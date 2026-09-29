"""Tests for the install command."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from cartograph import cli, skills, uninstall
from cartograph.cli import _handle_init


def _args(tmp_path: Path, platform: str) -> argparse.Namespace:
    """Install arguments for a test."""
    return argparse.Namespace(
        repo=str(tmp_path),
        dry_run=False,
        platform=platform,
        yes=True,
        no_instructions=True,
        no_skills=False,
        no_hooks=False,
    )


def test_copilot_install_reinstall_uninstall_lifecycle(
    monkeypatch, tmp_path
):
    """The public lifecycle migrates safely and is repeatable without a client."""
    repo = tmp_path / "repo"
    (repo / ".git" / "hooks").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    legacy_instruction = repo / ".github" / "cartograph.instruction.md"
    legacy_instruction.parent.mkdir(parents=True)
    legacy_instruction.write_text(
        "# User notes\n\n" + skills._COPILOT_SECTION,
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: home)
    args = _args(repo, "copilot")
    args.no_instructions = False
    args.no_skills = True
    args.no_hooks = True

    _handle_init(args)
    current_instruction = (
        repo
        / ".github"
        / "instructions"
        / "cartograph.instructions.md"
    )
    first_instruction = current_instruction.read_bytes()
    _handle_init(args)

    assert current_instruction.read_bytes() == first_instruction
    assert legacy_instruction.read_text(encoding="utf-8") == "# User notes\n"

    report = uninstall.run(repo=repo, keep_data=True)

    assert report.errors == []
    assert legacy_instruction.read_text(encoding="utf-8") == "# User notes\n"
    assert not current_instruction.exists()


def test_install_writes_no_mcp_config_by_default(monkeypatch, tmp_path):
    """Install must not register an MCP server.

    Cartograph exists because MCP servers are prohibited in the target
    environment. An install that writes .mcp.json breaks the one constraint the
    project is built around, so it is asserted, not assumed.
    """
    repo = tmp_path / "repo"
    (repo / ".git" / "hooks").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    _handle_init(_args(repo, "copilot"))

    written = [p for p in repo.rglob("*") if p.is_file() and "mcp" in p.name.lower()]
    assert written == [], f"install wrote MCP config: {written}"


def test_install_defaults_to_copilot_and_accepts_nothing_else():
    """The extension and the installers pass --platform copilot; nothing else exists."""
    captured = {}
    with patch.object(cli, "_handle_init", side_effect=lambda a: captured.setdefault("a", a)):
        with patch.object(sys, "argv", ["carto", "install", "--repo", "."]):
            cli.main()
    assert captured["a"].platform == "copilot"

    with patch.object(sys, "argv", ["carto", "install", "--platform", "claude"]):
        with pytest.raises(SystemExit) as exc:
            cli.main()
    assert exc.value.code == 1
