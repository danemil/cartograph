"""What ``carto install`` places in a repository for GitHub Copilot.

The skills pack in ``.github/skills/``, the hooks in
``.github/hooks/cartograph.json`` and the instruction file in
``.github/instructions/`` — each read by both Copilot CLI and Copilot Chat.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from ._legacy_instructions import LEGACY_INSTRUCTION_SECTIONS

logger = logging.getLogger(__name__)


# --- Skill file contents ---

#: The skills pack, loaded from package data rather than carried as Python
#: string literals.
#:
#: It used to be a dict of {name, description, body} that five installers each
#: reassembled into markdown. Three copies of the same content — this dict, the
#: bundled `skills/` tree, and the files actually written — had to be kept in
#: step by hand, and an upstream test exists precisely because they drifted.
#: They drifted again here: the bodies still instructed agents to call MCP
#: tools, which Cartograph does not have, and `install` overwrote the real
#: skills pack with them.
#:
#: So there is now one copy. `skills_data/` is synced from the repo's canonical
#: `skills/` by `scripts/install-skills.py`, and every installer writes the
#: file **verbatim** — no reassembly, so no divergence to test for.
_SKILLS_PACKAGE = "cartograph.skills_data"


@lru_cache(maxsize=1)
def skill_documents() -> dict[str, str]:
    """Return ``{slug: SKILL.md text}`` for the bundled skills pack."""
    from importlib import resources

    out: dict[str, str] = {}
    root = resources.files(_SKILLS_PACKAGE)
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        doc = entry / "SKILL.md"
        if doc.is_file():
            out[entry.name] = doc.read_text(encoding="utf-8")
    return out


def _parse_frontmatter(text: str) -> tuple[str, str]:
    """Split a SKILL.md into (description, body). Name comes from the slug."""
    description, body = "", text
    if text.startswith("---"):
        close = text.find("\n---", 3)
        if close != -1:
            for line in text[3:close].splitlines():
                key, _, value = line.partition(":")
                if key.strip() == "description":
                    description = value.strip()
            body = text[close + 4:].lstrip("\n")
    return description, body


def _skills_index() -> dict[str, dict[str, str]]:
    """Back-compat view of the pack, keyed by ``<slug>.md``.

    Kept because uninstall enumerates installed skills through it; the
    installers no longer use it, because reassembling a file from its parts is
    what this change removes.
    """
    index: dict[str, dict[str, str]] = {}
    for slug, text in skill_documents().items():
        description, body = _parse_frontmatter(text)
        index[f"{slug}.md"] = {"name": slug, "description": description, "body": body}
    return index


class _SkillsView(Mapping):
    """Lazy mapping so module import does not touch the filesystem."""

    def __iter__(self):
        return iter(_skills_index())

    def __len__(self) -> int:
        return len(_skills_index())

    def __getitem__(self, key: str) -> dict[str, str]:
        return _skills_index()[key]


_SKILLS: Mapping[str, dict[str, str]] = _SkillsView()


def _write_skills_pack(skills_dir: Path) -> Path:
    """Write the bundled skills pack into ``<dir>/<slug>/SKILL.md``.

    Every host that discovers Agent Skills uses this layout and this
    frontmatter, so there is one writer rather than one per host. The file is
    copied byte for byte: reassembling it from parsed parts is what let the
    installed skills drift from the pack in the first place.
    """
    skills_dir.mkdir(parents=True, exist_ok=True)
    for slug, text in skill_documents().items():
        target = skills_dir / slug
        target.mkdir(parents=True, exist_ok=True)
        path = target / "SKILL.md"
        path.write_text(text, encoding="utf-8")
        logger.info("Wrote skill: %s", path)
    return skills_dir


#: Where the pack is installed: the one project skills directory both Copilot
#: CLI (`copilot skill --help`, 1.0.82) and Copilot Chat (VS Code's agent
#: skills documentation) name. Each also reads `.claude/skills/` and
#: `.agents/skills/`, so nothing is gained by writing there too.
HOST_SKILL_DIR = ".github/skills"


def install_host_skills(repo_root: Path) -> Path:
    """Install the pack where Copilot CLI and Copilot Chat will find it."""
    return _write_skills_pack(repo_root / HOST_SKILL_DIR)


def hook_command(
    event: str, *, host: str | None = None, reads_payload: bool = False
) -> str:
    """One host hook command line, for the hosts that run a POSIX shell.

    The shell does three things and nothing more: consume the JSON the host
    pipes in, exit silently when the binary is not on ``$PATH`` (#549), and
    resolve the checkout at hook runtime so a committed hook file works for
    every collaborator (#558). The git guard still precedes the work, so a
    workspace root without a ``.git`` no-ops instead of erroring (#312).

    Every decision past that is ``carto hook``'s, in Python. A shell one-liner
    carrying logic is the version of this that cannot be tested, which is what
    it used to be.

    ``reads_payload`` is for an event whose entire input is that JSON. The
    drain moves to the end of the line, where it still runs if a guard
    short-circuits, instead of discarding the payload before ``carto`` can
    read it.

    ``host`` names the caller in the observations the event records. It is
    passed here because this is the only place that knows it: a payload does
    not say which host wrote it, and inferring one from its field names would
    be a guess stored as a fact.
    """
    return (
        ("" if reads_payload else "cat >/dev/null || true; ")
        # Guard on the binary this actually INVOKES. It used to test for
        # `cartograph`, the long alias, and then run `carto` — so an install
        # that put only `carto` on PATH, which is the primary entry point and
        # the name every remediation string uses, made every hook a silent
        # no-op. Silent is the whole problem: nothing fails, the graph simply
        # never updates.
        #
        # PATH gets the launcher directory appended first. The VS Code
        # extension puts it on the PATH of *terminals*, and a hook is not run
        # in one: VS Code runs Chat hooks in the extension host's environment,
        # and a CLI started outside VS Code has the login shell's. Measured in
        # both — without this every hook stopped at the guard below. Appended,
        # so a `carto` the person installed themselves still wins.
        + 'PATH="$PATH:${CARTO_HOME:-$HOME/.cartograph}/bin"; '
        + "command -v carto >/dev/null 2>&1 || exit 0; "
        + "git rev-parse --git-dir >/dev/null 2>&1"
        + f" && carto hook {event}"
        + (f" --host {host}" if host else "")
        + ' --repo "$(git rev-parse --show-toplevel 2>/dev/null)"'
        + (" || cat >/dev/null || true" if reads_payload else " || true")
    )


def powershell_hook_command(event: str, *, host: str | None = None) -> str:
    """The Windows form of :func:`hook_command`, for the same three guards.

    Present because a hook file committed to a repository is read on every
    collaborator's machine, and the POSIX line means nothing to PowerShell.
    ``$input`` forwards the host's payload; the events that do not read one
    ignore it. Never run on Windows yet — see ``docs/copilot-hooks.md``.
    """
    return (
        '$env:PATH += ";$(if ($env:CARTO_HOME) { $env:CARTO_HOME } else { "$HOME\\.cartograph" })\\bin"; '
        "if (-not (Get-Command carto -ErrorAction SilentlyContinue)) { exit 0 }; "
        "$r = git rev-parse --show-toplevel 2>$null; if (-not $r) { exit 0 }; "
        f"$input | carto hook {event}"
        + (f" --host {host}" if host else "")
        + ' --repo "$r"; exit 0'
    )


#: The moments Copilot hooks fire, and the job each is given. One table so the
#: POSIX and PowerShell forms cannot come to disagree about what runs when.
#:
#: - ``SessionStart`` summarises earlier sessions that ended without a
#:   ``SessionEnd`` — every VS Code Chat session, since VS Code has none. Not
#:   ``session-status``: that prints a line, and VS Code parses stdout as JSON.
#: - ``Stop`` refreshes the graph once per agent turn. ``PostToolUse`` would
#:   fire on every read as well as every edit, and VS Code ignores matchers,
#:   so a busy turn would start a dozen concurrent updates.
#: - ``SessionEnd`` fires only in the CLI; VS Code ignores the unknown key.
_COPILOT_HOOKS: tuple[tuple[str, str, str | None], ...] = (
    ("SessionStart", "session-catchup", None),
    ("UserPromptSubmit", "prompt-capture", "copilot"),
    ("Stop", "file-update", None),
    ("SessionEnd", "session-summarise", None),
)

#: The payload-reading jobs, which :func:`hook_command` must not drain first.
_READS_PAYLOAD = frozenset({"session-catchup", "prompt-capture", "session-summarise"})


def generate_copilot_hooks_config() -> dict[str, Any]:
    """``.github/hooks/cartograph.json``, read by Copilot CLI and Copilot Chat.

    One file in the VS Code schema, because both hosts load ``.github/hooks``
    and the CLI runs a file in either schema: a second, CLI-schema file would
    fire every CLI event twice. ``--host copilot`` is narrowed to ``copilot-cli``
    or ``copilot-chat`` by :func:`cartograph.hook.resolve_host`.

    ``windows`` is VS Code's key for the PowerShell form and ``powershell`` the
    CLI's; both are written because each host ignores the other's.
    """
    hooks: dict[str, list[dict[str, Any]]] = {}
    for event, job, host in _COPILOT_HOOKS:
        windows = powershell_hook_command(job, host=host)
        hooks[event] = [
            {
                "type": "command",
                "command": hook_command(job, host=host, reads_payload=job in _READS_PAYLOAD),
                "windows": windows,
                "powershell": windows,
                # A ceiling on starting a process: every job returns in well
                # under a second and hands anything slow to spawn_detached.
                "timeout": 10,
            }
        ]
    return {"hooks": hooks}


def install_copilot_hooks(repo_root: Path) -> Path:
    """Write ``.github/hooks/cartograph.json`` and return its path.

    Cartograph's own file, replaced whole on every install rather than merged:
    ``.github/hooks`` is a directory of independent files, so a team's hooks
    live beside this one and are never touched.
    """
    hooks_dir = repo_root / ".github" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    path = hooks_dir / "cartograph.json"
    path.write_text(
        json.dumps(generate_copilot_hooks_config(), indent=2) + "\n", encoding="utf-8"
    )
    return path


# NOTE: the marker still says "MCP tools" and must not change. It is how a
# reinstall finds and replaces a block written by an earlier version — and the
# blocks worth replacing most urgently are exactly the ones that described MCP
# tools. Changing it would leave those in place and append a second section.
_SECTION_MARKER = "<!-- cartograph MCP tools -->"

# Closes the managed block so reinstall can replace it without guessing where it
# ends. Releases before this shipped only the opening marker; those blocks are
# matched by their full text instead, see _legacy_instructions.
_SECTION_END_MARKER = "<!-- /cartograph MCP tools -->"

_INSTRUCTION_INTRO = """**This project has a knowledge graph. Query it with the `carto` CLI to
narrow scope, then read the source.** Cheaper than scanning files, and it gives you structural
context (callers, dependents, test coverage) that file search cannot.

There is no MCP server; `carto` is a plain command. The skills in `.github/skills/` are more
specific; prefer those — this is the fallback."""

_INSTRUCTION_GUARDRAILS = """### Verify in the source

- Narrow scope with the graph, then read the source. Do not change code from graph output alone.
- For any non-trivial change, read the implementation and the relevant tests before concluding.
- Verify the exact source when touching behavior, database logic, migrations, retries, fallbacks,
  recovery, or compatibility code.
- When the graph and the source disagree, the source wins. The graph may be stale or may not
  model that relationship.
- An empty graph result can mean "not indexed" or "not statically visible", not "does not exist"."""

# Every command here is checked against the live parser by
# contracts/capability-v1/check_skills.py, which is why this table can be
# trusted where the MCP tool list it replaced could not.
_INSTRUCTION_COMMANDS = """### Commands

Every one is checked against the live parser by the skills conformance suite,
which is why this table can be trusted where the tool list it replaced could not.

| Command | Use when |
| --- | --- |
| `carto review-summary --base <ref>` | First look at a change: risk, counts, test gaps |
| `carto review-context --base <ref>` | Full review context, once the summary warrants it |
| `carto impact --files <file>` | Blast radius of a change |
| `carto query callers_of <symbol>` | Callers, callees, imports, tests (16 patterns) |
| `carto search "<text>"` | Find code when you do not know the symbol name |
| `carto architecture` | Shape of an unfamiliar codebase |
| `carto refactor rename --old-name <a> --new-name <b>` | Plan a rename; preview only, never edits |
| `carto build` / `carto update` | Create or refresh the graph |

Pass `--format json` always, and `--max-tokens N` to bound a response (truncation is
semantic, so the JSON stays valid). Exit `2` is a precondition: run `error.remediation`,
then retry. Full reference: `carto capabilities --format json`."""


# YAML front matter so Copilot applies it across the workspace.
_COPILOT_SECTION = f"""---
applyTo: '**'
description: >-
  Use the carto CLI for token-efficient codebase
  exploration and code review.
---

{_SECTION_MARKER}
## Code knowledge graph: cartograph

{_INSTRUCTION_INTRO}

{_INSTRUCTION_GUARDRAILS}

{_INSTRUCTION_COMMANDS}

The graph auto-updates on file changes, via hooks.
{_SECTION_END_MARKER}
"""


#: The instruction file Copilot CLI and Copilot Chat both read.
INSTRUCTION_FILE = ".github/instructions/cartograph.instructions.md"

#: Where an older release wrote it. Reinstall removes only the exact generated
#: section and leaves any user-authored content intact.
LEGACY_INSTRUCTION_FILE = ".github/cartograph.instruction.md"


def _known_instruction_sections() -> tuple[str, ...]:
    """Every block text this project has ever generated, longest first.

    Longest first matters: a shorter variant that happens to be contained in a
    longer one must never win the match and leave the tail behind.
    """
    return tuple(
        sorted({_COPILOT_SECTION, *LEGACY_INSTRUCTION_SECTIONS}, key=len, reverse=True)
    )


def _upgrade_managed_block(existing: str, section: str) -> str | None:
    """Replace a previously generated block with ``section``.

    Only text that exactly equals a known generated block is ever rewritten, so
    anything the user wrote around it survives byte for byte. Blocks predating
    the end marker have no closing boundary, which is why nothing here searches
    for one; guessing where such a block stops would eat user content.

    Returns the new file content, or None when the marker is present but no
    known block is, meaning someone edited the block by hand.
    """
    # Look for stale blocks only in the text that is NOT already current.
    # One generated section can be a substring of another — the Copilot file is
    # the shared block with YAML front matter prepended — and matching against
    # the raw text would see the shared block inside the current one, call it
    # stale, and "upgrade" it by prepending a second copy of the front matter
    # on every reinstall.
    outside = existing.replace(section, "")
    stale = [
        block
        for block in _known_instruction_sections()
        if block != section and block in outside
    ]
    if not stale:
        return None
    # Anchor on the longest match, then drop any duplicate blocks an older
    # release left behind. Splitting around the anchor keeps the cleanup away
    # from the text being written in, which a plain str.replace would not.
    head = stale[0]
    index = existing.index(head)
    before, after = existing[:index], existing[index + len(head) :]
    for block in stale[1:]:
        before = before.replace(block, "")
        after = after.replace(block, "")
    if section in before or section in after:
        # The current block is already there; the stale ones were duplicates.
        return before + after
    return before + section + after


def _inject_instructions(file_path: Path, section: str) -> str:
    """Create, or upgrade in place, the managed instruction block in a file.

    Returns one of:

    - ``"created"``: the block was written for the first time, creating the
      file or appending to one that had no block.
    - ``"updated"``: an older generated block was replaced with the current one.
    - ``"unchanged"``: the file already holds the current block, byte for byte.
      Nothing is written, so repeated installs do not touch the file.
    - ``"conflict"``: the marker is present but the block matches nothing this
      project generated, so it was hand-edited. The file is left alone and the
      caller is expected to tell the user about it.
    """
    existing = ""
    if file_path.exists():
        existing = file_path.read_text(encoding="utf-8", errors="replace")

    if _SECTION_MARKER in existing:
        upgraded = _upgrade_managed_block(existing, section)
        if upgraded is None:
            if section in existing:
                logger.info("%s already holds the current instructions.", file_path.name)
                return "unchanged"
            logger.warning(
                "%s has a hand-edited carto section; leaving it alone.",
                file_path,
            )
            return "conflict"
        file_path.write_text(upgraded, encoding="utf-8")
        logger.info("Updated the carto instructions in %s", file_path)
        return "updated"

    separator = "\n" if existing and not existing.endswith("\n") else ""
    extra_newline = "\n" if existing else ""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(existing + separator + extra_newline + section, encoding="utf-8")
    logger.info("Appended the carto instructions to %s", file_path)
    return "created"


def _remove_legacy_instruction_file(path: Path) -> None:
    """Strip an exact generated section from a superseded instruction file."""
    if not path.exists():
        return
    content = path.read_text(encoding="utf-8", errors="replace")
    if _SECTION_MARKER not in content:
        return
    # Longest first, so removing a long block cannot leave the tail of a shorter
    # variant it contains. Anything not generated by this project is left alone.
    for section in _known_instruction_sections():
        content = content.replace(section, "")
    if _SECTION_MARKER in content:
        return
    if content.strip():
        path.write_text(content.rstrip() + "\n", encoding="utf-8")
        logger.info("Removed legacy instruction section from %s", path)
    else:
        path.unlink()
        logger.info("Removed legacy instruction file %s", path)


def inject_instruction_files(repo_root: Path) -> dict[str, str]:
    """Write the instruction file and report what happened.

    Maps the filename to ``"created"``, ``"updated"``, ``"unchanged"`` or
    ``"conflict"``, as documented on ``_inject_instructions``, so the install
    command can say whether it upgraded the file or it needs manual attention.
    """
    outcome = _inject_instructions(repo_root / INSTRUCTION_FILE, _COPILOT_SECTION)
    _remove_legacy_instruction_file(repo_root / LEGACY_INSTRUCTION_FILE)
    return {INSTRUCTION_FILE: outcome}
