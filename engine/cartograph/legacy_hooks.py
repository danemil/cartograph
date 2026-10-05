"""Hook entries that releases before 0.6.0 wrote to ``.claude/settings.json``.

``carto install --platform claude`` merged Cartograph's hooks into that file,
beside whatever the person had there. Copilot is now the only host, so those
entries only run ``carto`` inside Claude Code, where nothing reads the result.

An entry is removed only when its command is, character for character, one a
release wrote. ``legacy_hooks.json`` lists them: each revision's own
``generate_hooks_config`` run, from git history (12 commands across 9
revisions). A hook the person wrote, even one that runs ``carto``, never
matches. Anything else in the file — their hooks, permissions, other keys — is
left as it was, and the file is deleted only when Cartograph's entries were
all it held.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path

SETTINGS = ".claude/settings.json"


@lru_cache(maxsize=1)
def written_commands() -> frozenset[str]:
    raw = resources.files("cartograph").joinpath("legacy_hooks.json").read_text(encoding="utf-8")
    return frozenset(json.loads(raw))


@dataclass
class HookSweep:
    """What the sweep did to ``.claude/settings.json``, repo-relative."""

    removed: int = 0          # hook commands removed
    deleted_file: bool = False
    kept: str | None = None   # why the file was left unread or unchanged


def _is_cartograph(hook: object) -> bool:
    return (
        isinstance(hook, dict)
        and hook.get("command") in written_commands()
    )


def _strip(hooks: dict) -> tuple[dict, int]:
    """``hooks`` without Cartograph's commands, and how many were dropped."""
    removed = 0
    kept_events: dict = {}
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            kept_events[event] = entries
            continue
        kept_entries = []
        for entry in entries:
            inner = entry.get("hooks") if isinstance(entry, dict) else None
            if not isinstance(inner, list):
                kept_entries.append(entry)
                continue
            mine = [h for h in inner if _is_cartograph(h)]
            if not mine:
                kept_entries.append(entry)
                continue
            removed += len(mine)
            rest = [h for h in inner if not _is_cartograph(h)]
            if rest:
                kept_entries.append({**entry, "hooks": rest})
        if kept_entries:
            kept_events[event] = kept_entries
    return kept_events, removed


def sweep(repo_root: Path, *, dry_run: bool = False) -> HookSweep:
    result = HookSweep()
    path = repo_root / SETTINGS
    if not path.is_file() or path.is_symlink():
        return result
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        result.kept = f"could not be read as JSON ({exc.__class__.__name__})"
        return result
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks"), dict):
        return result

    hooks, result.removed = _strip(settings["hooks"])
    if not result.removed:
        return result
    rest = {k: v for k, v in settings.items() if k != "hooks"}
    if hooks:
        rest["hooks"] = hooks
    if not rest:
        result.deleted_file = True
        if not dry_run:
            path.unlink()
    elif not dry_run:
        # Key order is kept; only the hooks value changes.
        settings = {k: (hooks if k == "hooks" else v) for k, v in settings.items()
                    if k != "hooks" or hooks}
        path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    return result


def describe(result: HookSweep, *, dry_run: bool = False) -> list[str]:
    """What install prints."""
    if result.kept:
        return [f"Left {SETTINGS}: {result.kept}"]
    if not result.removed:
        return []
    verb = "Would remove" if dry_run else "Removed"
    what = (f"{SETTINGS} (it held only those)" if result.deleted_file
            else f"{SETTINGS}; your other settings are unchanged")
    return [
        f"{verb} {result.removed} Claude Code hook command"
        f"{'s' if result.removed != 1 else ''} an earlier Cartograph install "
        f"wrote: {what}"
    ]
