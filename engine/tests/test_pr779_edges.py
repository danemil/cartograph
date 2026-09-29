"""Skill templates must match the shipped pack, and must not mention MCP.

Inherited from upstream PR #779, which guarded drift between the generated and
bundled copies of the skills. Cartograph removed the drift instead: there is
one copy, written verbatim, so byte-identity now holds by construction.

The MCP-schema assertions are **inverted**. Upstream required every backticked
``*_tool`` in a skill to resolve to a registered ``@mcp.tool()``. Cartograph
has no MCP server, so a skill naming one sends the agent after something that
does not exist — which is exactly the defect these skills were rewritten to
fix. The test now forbids what it used to require.
"""

import re
from pathlib import Path

from cartograph.skills import _SKILLS, install_host_skills

REPO_ROOT = Path(__file__).parents[1]
#: Derived, not hand-listed: the pack is the source of truth, and a hand-kept
#: list is one more copy to drift.
CANONICAL_SKILLS = REPO_ROOT.parent / "skills"
SKILL_NAMES = sorted(p.parent.name for p in CANONICAL_SKILLS.glob("*/SKILL.md"))

_BACKTICK = re.compile(r"`([^`]+)`")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _backticked_identifiers(markdown: str) -> list[str]:
    """Leading identifier of every backticked span, e.g. `foo(task="x")` -> foo."""
    out = []
    for span in _BACKTICK.findall(markdown):
        m = _IDENT.match(span)
        if m:
            out.append(m.group(0))
    return out


def _all_skill_files(tmp_path: Path) -> list[Path]:
    generated = install_host_skills(tmp_path)
    files = []
    for name in SKILL_NAMES:
        files.append(generated / name / "SKILL.md")
        files.append(CANONICAL_SKILLS / name / "SKILL.md")
    return files


def test_generated_and_bundled_skills_byte_identical(tmp_path):
    """What `install` writes must be what the repo verified.

    `check_skills.py` validates the canonical pack against the live CLI; this
    check is what makes that guarantee reach an installed machine.
    """
    generated = install_host_skills(tmp_path)
    for name in SKILL_NAMES:
        gen = (generated / name / "SKILL.md").read_text(encoding="utf-8")
        bundled = (CANONICAL_SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
        assert gen == bundled, f"generated and bundled {name}/SKILL.md diverged"


def test_no_skill_references_an_mcp_tool(tmp_path):
    """Inverted from upstream: naming an MCP tool is now the failure.

    MCP servers are banned in the target environment and Cartograph ships
    none, so an agent told to call `get_minimal_context_tool` has been sent
    after something that cannot exist.
    """
    for skill_file in _all_skill_files(tmp_path):
        content = skill_file.read_text(encoding="utf-8")
        for ident in _backticked_identifiers(content):
            assert not ident.endswith("_tool"), (
                f"{skill_file} references `{ident}`, an MCP tool name; "
                "skills must invoke the carto CLI instead"
            )
        assert "MCP" not in content, f"{skill_file} mentions MCP"


# REMOVED: test_no_bare_name_of_any_exported_tool.
#
# It forbade any backticked identifier matching an MCP tool's bare name. Now
# that skills invoke the CLI, `carto refactor` trips it — `refactor` is both a
# real subcommand and the bare form of `refactor_tool`, so the test fails on
# exactly the content it should approve. test_no_skill_references_an_mcp_tool
# above covers the real risk (a `*_tool` name, or the word MCP) without the
# false positives.


def test_install_host_skills_unicode_and_space_path(tmp_path):
    target = tmp_path / "üñí code (v2)" / "deep" / "nested"
    out = install_host_skills(target)
    assert out == target / ".github" / "skills"
    for name in SKILL_NAMES:
        content = (out / name / "SKILL.md").read_text(encoding="utf-8")
        assert "carto " in content
        assert content.startswith("---\n")


def test_install_host_skills_overwrites_stale_content(tmp_path):
    out = install_host_skills(tmp_path)
    stale = out / "debug-issue" / "SKILL.md"
    stale.write_text("Use `get_flow` and `get_minimal_context`.\n", encoding="utf-8")
    out2 = install_host_skills(tmp_path)
    assert out2 == out
    refreshed = stale.read_text(encoding="utf-8")
    assert "`get_flow`" not in refreshed
    assert "carto query" in refreshed


def test_skills_dict_covers_exactly_the_canonical_pack():
    assert sorted(f.removesuffix(".md") for f in _SKILLS) == SKILL_NAMES
