"""Tests for what ``carto install`` places in a repository for GitHub Copilot."""

import argparse
from pathlib import Path
from unittest.mock import patch

from cartograph import skills as skills_module
from cartograph.skills import (
    _SECTION_MARKER,
    INSTRUCTION_FILE,
    inject_instruction_files,
    install_host_skills,
)

CANONICAL_SKILLS = Path(__file__).parents[2] / "skills"
PACK_NAMES = sorted(p.parent.name for p in CANONICAL_SKILLS.glob("*/SKILL.md"))


class TestInstallHostSkills:
    def test_writes_the_pack_where_copilot_reads_it(self, tmp_path):
        result = install_host_skills(tmp_path)
        assert result == tmp_path / ".github" / "skills"
        assert result.is_dir()

    def test_writes_nowhere_else(self, tmp_path):
        """Copilot also reads these two, so a copy there is only a second copy."""
        install_host_skills(tmp_path)
        assert not (tmp_path / ".claude").exists()
        assert not (tmp_path / ".agents").exists()

    def test_creates_a_subdir_per_pack_skill(self, tmp_path):
        skills_dir = install_host_skills(tmp_path)
        subdirs = sorted(f.name for f in skills_dir.iterdir() if f.is_dir())
        assert subdirs == PACK_NAMES
        for d in skills_dir.iterdir():
            assert (d / "SKILL.md").is_file()

    def test_skill_files_have_frontmatter(self, tmp_path):
        skills_dir = install_host_skills(tmp_path)
        for subdir in skills_dir.iterdir():
            content = (subdir / "SKILL.md").read_text()
            assert content.startswith("---\n")
            assert "name:" in content
            assert "description:" in content
            assert content.index("---", 4) > 0

    def test_skill_frontmatter_names_match_lowercase_directories(self, tmp_path):
        """Installed and bundled skills use the discovery-safe name format."""
        installed = install_host_skills(tmp_path)

        for skill_name in PACK_NAMES:
            for skill_file in (
                installed / skill_name / "SKILL.md",
                CANONICAL_SKILLS / skill_name / "SKILL.md",
            ):
                content = skill_file.read_text(encoding="utf-8")
                assert f"\nname: {skill_name}\n" in content

    def test_every_skill_invokes_the_cli_and_never_an_mcp_tool(self, tmp_path):
        """Replaces two upstream tests that required MCP tool names.

        They asserted every skill mentioned `get_minimal_context_tool` and a
        `detail_level` argument. Cartograph ships no MCP server, so both now
        describe something an agent cannot call. What matters instead is that
        each skill drives the CLI.
        """
        skills_dir = install_host_skills(tmp_path)
        for subdir in skills_dir.iterdir():
            content = (subdir / "SKILL.md").read_text()
            assert "carto " in content, f"{subdir.name} invokes no carto command"
            assert "_tool" not in content, f"{subdir.name} names an MCP tool"

    def test_idempotent(self, tmp_path):
        install_host_skills(tmp_path)
        skills_dir = install_host_skills(tmp_path)
        assert len(list(skills_dir.iterdir())) == len(PACK_NAMES)


class TestInjectInstructions:
    def _file(self, tmp_path: Path) -> Path:
        return tmp_path / INSTRUCTION_FILE

    def test_creates_section_in_new_file(self, tmp_path):
        inject_instruction_files(tmp_path)
        content = self._file(tmp_path).read_text()
        assert content.startswith("---\napplyTo: '**'\n")
        assert _SECTION_MARKER in content
        assert "## Code knowledge graph" in content

    def test_appends_to_existing_file(self, tmp_path):
        path = self._file(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("# My Project\n\nExisting content.\n")

        inject_instruction_files(tmp_path)

        content = path.read_text()
        assert "# My Project" in content
        assert "Existing content." in content
        assert _SECTION_MARKER in content

    def test_idempotent(self, tmp_path):
        inject_instruction_files(tmp_path)
        first = self._file(tmp_path).read_text()

        inject_instruction_files(tmp_path)
        second = self._file(tmp_path).read_text()

        assert first == second
        assert second.count(_SECTION_MARKER) == 1

    def test_idempotent_with_existing_content(self, tmp_path):
        path = self._file(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("# Existing\n")

        inject_instruction_files(tmp_path)
        first = path.read_text()
        inject_instruction_files(tmp_path)

        assert path.read_text() == first
        assert first.count(_SECTION_MARKER) == 1

    def test_writes_no_other_instruction_file(self, tmp_path):
        inject_instruction_files(tmp_path)
        written = sorted(
            str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()
        )
        assert written == [str(Path(INSTRUCTION_FILE))]


def _legacy_copilot_sections() -> list[str]:
    """Recorded past blocks for the Copilot file, longest first."""
    return [
        block
        for block in skills_module.LEGACY_INSTRUCTION_SECTIONS
        if block.startswith("---\n")
    ]


class TestManagedBlockUpgrade:
    """Reinstall must replace an older generated block, not silently skip it.

    Regression test for #314: the injector only checked whether the opening
    marker was present, so anyone who installed before the guardrails landed
    kept the old text forever and reinstalling was a no-op.
    """

    OLDER = _legacy_copilot_sections()[0]

    def _file(self, tmp_path: Path) -> Path:
        path = tmp_path / INSTRUCTION_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_reinstall_upgrades_an_older_generated_section(self, tmp_path):
        path = self._file(tmp_path)
        path.write_text(self.OLDER, encoding="utf-8")

        assert inject_instruction_files(tmp_path) == {INSTRUCTION_FILE: "updated"}

        content = path.read_text(encoding="utf-8")
        assert content == skills_module._COPILOT_SECTION
        assert self.OLDER not in content
        assert "### Verify in the source" in content
        assert content.count(_SECTION_MARKER) == 1

    def test_every_recorded_copilot_block_upgrades(self, tmp_path):
        for block in _legacy_copilot_sections():
            path = self._file(tmp_path)
            path.write_text(block, encoding="utf-8")

            assert inject_instruction_files(tmp_path) == {INSTRUCTION_FILE: "updated"}
            content = path.read_text(encoding="utf-8")
            assert content == skills_module._COPILOT_SECTION
            assert "ALWAYS use the" not in content

    def test_reinstall_over_current_section_is_byte_idempotent(self, tmp_path):
        assert inject_instruction_files(tmp_path) == {INSTRUCTION_FILE: "created"}
        path = tmp_path / INSTRUCTION_FILE
        first = path.read_bytes()
        stat_before = path.stat().st_mtime_ns

        assert inject_instruction_files(tmp_path) == {INSTRUCTION_FILE: "unchanged"}

        assert path.read_bytes() == first
        # "unchanged" must not rewrite the file at all.
        assert path.stat().st_mtime_ns == stat_before

    def test_hand_edited_block_is_preserved_and_reported(self, tmp_path):
        edited = self.OLDER.replace("### ", "### (our notes) ", 1)
        assert edited != self.OLDER
        path = self._file(tmp_path)
        path.write_text(edited, encoding="utf-8")

        assert inject_instruction_files(tmp_path) == {INSTRUCTION_FILE: "conflict"}

        assert path.read_text(encoding="utf-8") == edited

    def test_user_content_around_the_block_survives_an_upgrade(self, tmp_path):
        head = "# House rules\n\nNever force push.\n\n"
        tail = "\n## Deploy notes\n\nRun the migration first.\n"
        path = self._file(tmp_path)
        path.write_text(head + self.OLDER + tail, encoding="utf-8")

        assert inject_instruction_files(tmp_path) == {INSTRUCTION_FILE: "updated"}

        content = path.read_text(encoding="utf-8")
        assert content == head + skills_module._COPILOT_SECTION + tail

    def test_duplicate_stale_blocks_collapse_to_one(self, tmp_path):
        """#558 left repeat installs stacking blocks; upgrade must not keep both."""
        older_two = _legacy_copilot_sections()[1]
        path = self._file(tmp_path)
        path.write_text(self.OLDER + "\n" + older_two, encoding="utf-8")

        assert inject_instruction_files(tmp_path) == {INSTRUCTION_FILE: "updated"}

        content = path.read_text(encoding="utf-8")
        assert content.count(_SECTION_MARKER) == 1
        assert skills_module._COPILOT_SECTION in content

    def test_new_sections_carry_an_end_marker(self, tmp_path):
        end = skills_module._SECTION_END_MARKER
        inject_instruction_files(tmp_path)

        content = (tmp_path / INSTRUCTION_FILE).read_text(encoding="utf-8")
        assert content.count(end) == 1
        assert content.index(_SECTION_MARKER) < content.index(end)

    def test_legacy_sections_are_exact_and_ordered_longest_first(self):
        legacy = skills_module.LEGACY_INSTRUCTION_SECTIONS
        assert len(set(legacy)) == len(legacy)
        assert all(_SECTION_MARKER in block for block in legacy)
        assert list(legacy) == sorted(legacy, key=len, reverse=True)
        known = skills_module._known_instruction_sections()
        assert list(known) == sorted(known, key=len, reverse=True)
        assert skills_module._COPILOT_SECTION in known


class TestInjectInstructionFilesOutcomes:
    def test_reports_created_then_unchanged(self, tmp_path):
        assert inject_instruction_files(tmp_path) == {INSTRUCTION_FILE: "created"}
        assert inject_instruction_files(tmp_path) == {INSTRUCTION_FILE: "unchanged"}

    def test_reports_conflict_without_touching_the_file(self, tmp_path):
        edited = _SECTION_MARKER + "\n## Our own rules\n"
        path = tmp_path / INSTRUCTION_FILE
        path.parent.mkdir(parents=True)
        path.write_text(edited, encoding="utf-8")

        assert inject_instruction_files(tmp_path) == {INSTRUCTION_FILE: "conflict"}
        assert path.read_text(encoding="utf-8") == edited


class TestInstructionGuardrails:
    """The generated instruction file must carry the source-verification guardrails.

    Regression test for #314: the generated text used to tell agents to ALWAYS use
    the graph before reading source and to fall back to file search ONLY when the
    graph did not cover the need, which made models act on graph summaries alone.
    """

    # Each guardrail is asserted by a fragment short enough to survive rewrapping.
    GUARDRAIL_FRAGMENTS = (
        "Do not change code from graph output alone",
        "read the implementation and the relevant tests before concluding",
        "migrations, retries, fallbacks",
        "the source wins",
        'can mean "not indexed" or "not statically visible"',
    )

    @staticmethod
    def _written(tmp_path: Path) -> str:
        inject_instruction_files(tmp_path)
        return (tmp_path / INSTRUCTION_FILE).read_text(encoding="utf-8")

    def test_the_instruction_file_has_all_guardrails(self, tmp_path):
        content = self._written(tmp_path)
        assert "### Verify in the source" in content
        for fragment in self.GUARDRAIL_FRAGMENTS:
            assert fragment in content, f"missing: {fragment}"

    def test_the_instruction_file_does_not_claim_the_graph_replaces_source(
        self, tmp_path
    ):
        content = self._written(tmp_path)
        assert "ALWAYS use the" not in content
        assert "**only** when the graph" not in content

    def test_shared_text_is_what_is_written(self, tmp_path):
        content = self._written(tmp_path)
        assert skills_module._INSTRUCTION_GUARDRAILS in content
        assert skills_module._INSTRUCTION_INTRO in content

    def test_guardrails_stay_small(self):
        """The section ships in every user's context, so cap its growth."""
        assert skills_module._INSTRUCTION_GUARDRAILS.count("\n") + 1 <= 9
        assert skills_module._COPILOT_SECTION.count("\n") <= 53

    def test_skill_templates_do_not_demand_graph_only_work(self):
        for filename, skill in skills_module._SKILLS.items():
            body = skill["body"]
            assert "ALWAYS start with" not in body, filename
            # Upstream demanded one exact sentence. The point is the guardrail,
            # not the wording: a skill must not present the graph as a
            # substitute for reading the code.
            assert "read" in body.lower(), filename


class TestLegacyInstructionFile:
    """An older release wrote ``.github/cartograph.instruction.md``."""

    LEGACY = Path(".github") / "cartograph.instruction.md"

    def test_reinstall_migrates_generated_legacy_instruction(self, tmp_path):
        """Reinstall removes only generated content from the superseded path."""
        legacy = tmp_path / self.LEGACY
        legacy.parent.mkdir(parents=True)
        legacy.write_text(
            "# User notes\n\n" + skills_module._COPILOT_SECTION,
            encoding="utf-8",
        )

        inject_instruction_files(tmp_path)

        assert legacy.read_text(encoding="utf-8") == "# User notes\n"
        assert (tmp_path / INSTRUCTION_FILE).exists()

    def test_reinstall_deletes_generated_only_legacy_instruction(self, tmp_path):
        legacy = tmp_path / self.LEGACY
        legacy.parent.mkdir(parents=True)
        legacy.write_text(skills_module._COPILOT_SECTION, encoding="utf-8")

        inject_instruction_files(tmp_path)

        assert not legacy.exists()

    def test_reinstall_leaves_user_legacy_instruction_untouched(self, tmp_path):
        """A user-authored file without the marker is never rewritten."""
        legacy = tmp_path / self.LEGACY
        legacy.parent.mkdir(parents=True)
        legacy.write_text("# User instructions\n", encoding="utf-8")

        inject_instruction_files(tmp_path)

        assert legacy.read_text(encoding="utf-8") == "# User instructions\n"


class TestInstallPlacesOnlyCopilotFiles:
    """``carto install`` writes Copilot's files and nothing another host reads."""

    def _run_install(self, tmp_path: Path, **overrides) -> None:
        from cartograph import cli as crg_cli

        args = argparse.Namespace(
            command="install",
            repo=str(tmp_path),
            platform="copilot",
            yes=True,
            dry_run=False,
            no_skills=False,
            no_hooks=False,
            no_instructions=False,
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        with patch("builtins.input", return_value="n"):
            with patch("pathlib.Path.home", return_value=tmp_path / "home"):
                crg_cli._handle_init(args)

    def test_writes_skills_hooks_and_instructions(self, tmp_path):
        self._run_install(tmp_path)
        assert (tmp_path / ".github" / "skills").is_dir()
        assert (tmp_path / ".github" / "hooks" / "cartograph.json").is_file()
        assert (tmp_path / INSTRUCTION_FILE).is_file()

    def test_writes_nothing_for_another_host(self, tmp_path):
        self._run_install(tmp_path)
        for other in (
            ".claude", ".agents", ".gemini", ".codebuddy", ".qoder", ".kiro",
            ".cursor", ".vscode", "CLAUDE.md", "AGENTS.md", "GEMINI.md",
            ".mcp.json", ".cursorrules", ".windsurfrules",
        ):
            assert not (tmp_path / other).exists(), other
        assert not (tmp_path / "home").exists()

    def test_opt_outs_skip_each_file(self, tmp_path):
        self._run_install(
            tmp_path, no_skills=True, no_hooks=True, no_instructions=True,
        )
        assert not (tmp_path / ".github").exists()


class TestSkillRouting:
    """A host picks a skill by its description alone, and every description is
    sent with every request. These pin the routing a measured run got wrong —
    "who calls X, and what would break" was answered with grep — and the size."""

    @staticmethod
    def _frontmatter() -> dict[str, dict]:
        import yaml

        out = {}
        for slug, text in skills_module.skill_documents().items():
            out[slug] = yaml.safe_load(text.split("---", 2)[1])
        return out

    def test_frontmatter_is_valid_yaml_with_name_and_description(self):
        for slug, fm in self._frontmatter().items():
            assert fm["name"] == slug
            assert isinstance(fm["description"], str) and fm["description"]

    def test_graph_questions_route_to_the_skill_that_runs_callers_and_impact(self):
        docs = skills_module.skill_documents()
        owners = [
            slug for slug, text in docs.items()
            if "carto query callers_of" in text and "carto impact" in text
            and "who calls" in self._frontmatter()[slug]["description"].lower()
        ]
        assert owners == ["refactor-safely"]
        description = self._frontmatter()["refactor-safely"]["description"].lower()
        for phrase in ("who calls", "would break", "where it is used",
                       "blast radius", "impact of", "signature"):
            assert phrase in description, phrase

    def test_descriptions_stay_small(self):
        # 1,849 characters before the routing fix; every one is paid per request.
        total = sum(len(fm["description"]) for fm in self._frontmatter().values())
        assert total <= 1400, total
