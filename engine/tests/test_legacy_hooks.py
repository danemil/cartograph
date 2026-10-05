"""Cartograph's old Claude Code hooks go; everything else in the file stays."""

import json
from pathlib import Path

from cartograph.legacy_hooks import SETTINGS, describe, sweep

# Verbatim from releases before 0.6.0: generate_hooks_config's hook_command
# (9a1ad15), and the inline form of the first releases (84f6d4a).
_HOOK = (
    'cat >/dev/null || true; PATH="$PATH:${CARTO_HOME:-$HOME/.cartograph}/bin"; '
    "command -v carto >/dev/null 2>&1 || exit 0; "
    "git rev-parse --git-dir >/dev/null 2>&1 && carto hook {event}"
    ' --repo "$(git rev-parse --show-toplevel 2>/dev/null)" || true'
)
_SUMMARISE = (
    'PATH="$PATH:${CARTO_HOME:-$HOME/.cartograph}/bin"; '
    "command -v carto >/dev/null 2>&1 || exit 0; "
    "git rev-parse --git-dir >/dev/null 2>&1 && carto hook session-summarise"
    ' --host claude-code --repo "$(git rev-parse --show-toplevel 2>/dev/null)"'
    " || cat >/dev/null || true"
)
_OLD = (
    "cat >/dev/null || true; command -v cartograph >/dev/null 2>&1 || exit 0; "
    "git rev-parse --git-dir >/dev/null 2>&1 && carto update --skip-flows"
    ' --repo "$(git rev-parse --show-toplevel 2>/dev/null)" || true'
)


def _entry(command: str, matcher: str = "") -> dict:
    return {"matcher": matcher, "hooks": [{"type": "command", "command": command}]}


def _write(root: Path, data: dict) -> Path:
    path = root / SETTINGS
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def test_a_file_holding_only_cartographs_hooks_is_deleted(tmp_path: Path):
    path = _write(tmp_path, {"hooks": {
        "SessionStart": [_entry(_HOOK.replace("{event}", "session-status"))],
        "PostToolUse": [_entry(_OLD, "Edit|Write")],
    }})
    result = sweep(tmp_path)
    assert result.removed == 2 and result.deleted_file
    assert not path.exists()
    assert (tmp_path / ".claude").is_dir()  # other Claude files may live there
    assert "it held only those" in describe(result)[0]


def test_the_persons_own_hooks_and_settings_survive(tmp_path: Path):
    own = {"type": "command", "command": "npm run lint"}
    path = _write(tmp_path, {
        "permissions": {"allow": ["Bash(ls)"]},
        "hooks": {
            "PostToolUse": [
                {"matcher": "Edit|Write", "hooks": [
                    {"type": "command", "command": _HOOK.replace("{event}", "file-update")},
                    own,
                ]},
            ],
            "SessionEnd": [_entry(_SUMMARISE)],
            "Stop": [_entry("echo done")],
        },
    })
    result = sweep(tmp_path)
    assert result.removed == 2 and not result.deleted_file
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "permissions": {"allow": ["Bash(ls)"]},
        "hooks": {
            "PostToolUse": [{"matcher": "Edit|Write", "hooks": [own]}],
            "Stop": [_entry("echo done")],
        },
    }


def test_a_command_that_only_mentions_carto_is_not_cartographs(tmp_path: Path):
    data = {"hooks": {"Stop": [_entry("carto status; echo hi")]}}
    path = _write(tmp_path, data)
    assert sweep(tmp_path).removed == 0
    assert json.loads(path.read_text(encoding="utf-8")) == data


def test_a_hand_written_guarded_carto_hook_is_kept(tmp_path: Path):
    # Same guard, same command, written by a person: not a release's text.
    own = "git rev-parse --git-dir >/dev/null 2>&1 && carto update --skip-flows || true"
    data = {"hooks": {"PostToolUse": [_entry(own, "Edit|Write")]}}
    path = _write(tmp_path, data)
    assert sweep(tmp_path).removed == 0
    assert json.loads(path.read_text(encoding="utf-8")) == data


def test_every_listed_command_is_one_a_release_wrote():
    from cartograph.legacy_hooks import written_commands
    assert _HOOK.replace("{event}", "session-status") in written_commands()
    assert _OLD in written_commands()
    assert _SUMMARISE in written_commands()
    assert len(written_commands()) == 12


def test_dry_run_changes_nothing(tmp_path: Path):
    path = _write(tmp_path, {"hooks": {"SessionStart": [_entry(_OLD)]}})
    before = path.read_bytes()
    result = sweep(tmp_path, dry_run=True)
    assert result.removed == 1 and result.deleted_file
    assert path.read_bytes() == before
    assert describe(result, dry_run=True)[0].startswith("Would remove 1 ")


def test_unreadable_json_is_left_and_said(tmp_path: Path):
    path = tmp_path / SETTINGS
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")
    result = sweep(tmp_path)
    assert path.read_text(encoding="utf-8") == "{ not json"
    assert describe(result) == [f"Left {SETTINGS}: could not be read as JSON (JSONDecodeError)"]


def test_no_file_no_output(tmp_path: Path):
    assert describe(sweep(tmp_path)) == []
