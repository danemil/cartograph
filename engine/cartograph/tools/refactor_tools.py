"""Tool 17: refactor_func."""

from __future__ import annotations

from typing import Any

from ..hints import generate_hints, get_session
from ..refactor import (
    find_dead_code,
    rename_preview,
    suggest_refactorings,
)
from ._common import (
    _bounded,
    _get_store,
    _shown_of,
    _validate_positive_int,
)

# ---------------------------------------------------------------------------
# Tool 17: refactor_tool  [REFACTOR]
# ---------------------------------------------------------------------------

# Hard ceiling. Dead-code and suggestion lists grow with the repository:
# an uncapped ``dead_code`` sweep returned ~47k tokens on this repo alone.
_MAX_REFACTOR_RESULTS = 150

_MINIMAL_DEAD_FIELDS = ("name", "kind", "relative_path", "line")
_MINIMAL_SUGGESTION_FIELDS = ("type", "description", "symbols")
_MINIMAL_EDIT_FIELDS = ("file", "line", "confidence")


def _project(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """Keep only *fields* on each row, dropping keys the row does not have."""
    return [{k: r[k] for k in fields if k in r} for r in rows]


def refactor_func(
    mode: str = "rename",
    old_name: str | None = None,
    new_name: str | None = None,
    kind: str | None = None,
    file_pattern: str | None = None,
    repo_root: str | None = None,
    max_results: int = 50,
    detail_level: str = "standard",
) -> dict[str, Any]:
    """Unified refactoring entry point.

    [REFACTOR] Supports three modes:
    - ``rename``: Preview renaming a symbol (requires *old_name* and
      *new_name*).
    - ``dead_code``: Find unreferenced functions/classes.
    - ``suggest``: Get community-driven refactoring suggestions.

    Args:
        mode: One of ``"rename"``, ``"dead_code"``, or ``"suggest"``.
        old_name: (rename mode) Current symbol name.
        new_name: (rename mode) Desired new name.
        kind: (dead_code mode) Optional node kind filter.
        file_pattern: (dead_code mode) Optional file path substring filter.
        repo_root: Repository root path. Auto-detected if omitted.
        max_results: Maximum edits/symbols/suggestions to include in the
            response (default 50, capped at 150). ``total`` always reports
            the untruncated count.
        detail_level: "standard" (default) returns full records; "minimal"
            keeps only the identifying fields per record.

    Returns:
        Mode-specific results dict with ``total`` and ``truncated``.
    """
    valid_modes = {"rename", "dead_code", "suggest"}
    if mode not in valid_modes:
        return {
            "status": "error",
            "error": (
                f"Invalid mode '{mode}'. "
                f"Must be one of: {', '.join(sorted(valid_modes))}"
            ),
        }
    _validate_positive_int(max_results, "max_results")

    store, root = _get_store(repo_root)
    try:
        if mode == "rename":
            if not old_name or not new_name:
                return {
                    "status": "error",
                    "error": (
                        "rename mode requires both old_name and new_name."
                    ),
                }
            preview = rename_preview(store, old_name, new_name)
            if preview is None:
                return {
                    "status": "not_found",
                    "summary": f"No node found matching '{old_name}'.",
                }
            edits, total, truncated = _bounded(
                preview["edits"], max_results, _MAX_REFACTOR_RESULTS,
            )
            if detail_level == "minimal":
                edits = _project(edits, _MINIMAL_EDIT_FIELDS)
            result = {
                "status": "ok",
                "summary": (
                    f"Rename preview: {old_name} -> {new_name}, "
                    f"{total} edit(s)"
                    + _shown_of(len(edits), total) + "."
                ),
                **preview,
                "edits": edits,
                "total": total,
                "truncated": truncated,
            }
            result["_hints"] = generate_hints(
                "refactor", result, get_session()
            )
            return result

        elif mode == "dead_code":
            dead, total, truncated = _bounded(
                find_dead_code(
                    store, kind=kind, file_pattern=file_pattern, root=root
                ),
                max_results,
                _MAX_REFACTOR_RESULTS,
            )
            if detail_level == "minimal":
                dead = _project(dead, _MINIMAL_DEAD_FIELDS)
            result = {
                "status": "ok",
                "summary": (
                    f"Found {total} dead code symbol(s)"
                    + _shown_of(len(dead), total) + "."
                ),
                "dead_code": dead,
                "total": total,
                "truncated": truncated,
            }
            result["_hints"] = generate_hints(
                "refactor", result, get_session()
            )
            return result

        else:  # suggest
            suggestions, total, truncated = _bounded(
                suggest_refactorings(store), max_results, _MAX_REFACTOR_RESULTS,
            )
            if detail_level == "minimal":
                suggestions = _project(suggestions, _MINIMAL_SUGGESTION_FIELDS)
            result = {
                "status": "ok",
                "summary": (
                    f"Generated {total} refactoring suggestion(s)"
                    + _shown_of(len(suggestions), total) + "."
                ),
                "suggestions": suggestions,
                "total": total,
                "truncated": truncated,
            }
            result["_hints"] = generate_hints(
                "refactor", result, get_session()
            )
            return result

    except Exception as exc:
        return {"status": "error", "error": str(exc)}
    finally:
        store.close()
