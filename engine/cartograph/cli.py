"""CLI entry point for cartograph.

Usage:
    carto install
    carto init
    carto uninstall [--dry-run] [--yes] [--repo PATH]
    carto build [--base BASE]
    carto update [--base BASE]
    carto forget PATH [PATH ...] [--dry-run]
    carto watch
    carto status
    carto visualize
    carto wiki
    carto detect-changes [--base BASE] [--brief]
    carto hook <event> [--repo PATH]
    carto register <path> [--alias name]
    carto unregister <path_or_alias>
    carto repos
    carto daemon start [--foreground]
    carto daemon stop
    carto daemon restart [--foreground]
    carto daemon status
    carto daemon logs [--repo ALIAS] [--follow] [--lines N]
    carto daemon add <path> [--alias NAME]
    carto daemon remove <path_or_alias>
"""

from __future__ import annotations

import sys

# Python version check — must come before any other imports
if sys.version_info < (3, 10):
    print("carto requires Python 3.10 or higher.")
    print(f"  You are running Python {sys.version}")
    print()
    print("Install Python 3.10+: https://www.python.org/downloads/")
    sys.exit(1)

import argparse
import fnmatch
import json
import logging
import os
from functools import partial
from pathlib import Path
from typing import Iterable, TypedDict

from .constants import GRAMMAR_PROBE_FLAG, QUERY_PATTERNS

logger = logging.getLogger(__name__)

# GitHub Copilot is the only host: one target covers Copilot CLI and Copilot
# Chat, which read the same files. The flag stays because the extension and the
# installers pass it.
_PLATFORM_CHOICES = ["copilot"]


class _EmbeddingRefreshKwargs(TypedDict, total=False):
    embedding_provider: str
    embedding_model: str


def _get_version() -> str:
    """The Cartograph release, as the banner and the catalogue report it."""
    from .release import release_version

    return release_version()


def _supports_color() -> bool:
    """Check if the terminal likely supports ANSI colors."""
    if os.environ.get("NO_COLOR"):
        return False
    if not hasattr(sys.stdout, "isatty"):
        return False
    return sys.stdout.isatty()


def _configure_utf8_stdio() -> None:
    """Allow Unicode CLI decoration on streams using a legacy encoding."""
    for stream in (sys.stdout, sys.stderr):
        encoding = getattr(stream, "encoding", None)
        if not encoding or encoding.lower().replace("-", "") == "utf8":
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass


def _print_banner() -> None:
    """Print the startup banner with graph art and available commands."""
    color = _supports_color()
    version = _get_version()

    # ANSI escape codes
    c = "\033[36m" if color else ""  # cyan — graph art
    y = "\033[33m" if color else ""  # yellow — center node
    b = "\033[1m" if color else ""  # bold
    d = "\033[2m" if color else ""  # dim
    g = "\033[32m" if color else ""  # green — commands
    r = "\033[0m" if color else ""  # reset

    print(f"""
{c}  ●──●──●{r}
{c}  │╲ │ ╱│{r}       {b}cartograph{r}  {d}v{version}{r}
{c}  ●──{y}◆{c}──●{r}
{c}  │╱ │ ╲│{r}       {d}Structural knowledge graph for{r}
{c}  ●──●──●{r}       {d}smarter code reviews{r}

  {b}Commands:{r}
    {g}install{r}     Place skills, hooks and instructions for GitHub Copilot
    {g}init{r}        Alias for install
    {g}build{r}       Full graph build {d}(parse all files){r}
    {g}update{r}      Incremental update {d}(changed files only){r}
    {g}watch{r}       Auto-update on file changes
    {g}status{r}      Show graph statistics
    {g}visualize{r}   Generate interactive HTML graph
    {g}wiki{r}        Generate markdown wiki from communities
    {g}detect-changes{r} Analyze change impact {d}(risk-scored review){r}
    {g}register{r}    Register a repository in the multi-repo registry
    {g}unregister{r}  Remove a repository from the registry
    {g}repos{r}       List registered repositories
    {g}postprocess{r} Run post-processing {d}(flows, communities, FTS){r}
    {g}daemon{r}      Multi-repo watch daemon management
    {g}eval{r}        Run evaluation benchmarks

  {d}Run{r} {b}cartograph <command> --help{r} {d}for details{r}
""")


def _instruction_files_to_modify(repo_root: Path) -> list[str]:
    """Return the instruction file ``install`` would write or modify, if any.

    Used for the dry-run / confirm preview (#173). A file holding a section
    from an older release is listed as ``(update)``: install replaces that
    block in place rather than leaving it stale (#314).
    """
    from .skills import (
        _COPILOT_SECTION,
        _SECTION_MARKER,
        INSTRUCTION_FILE,
        _upgrade_managed_block,
    )

    path = repo_root / INSTRUCTION_FILE
    if not path.exists():
        return [f"{INSTRUCTION_FILE} (new)"]
    content = path.read_text(encoding="utf-8", errors="replace")
    if _SECTION_MARKER not in content:
        return [f"{INSTRUCTION_FILE} (append)"]
    if _upgrade_managed_block(content, _COPILOT_SECTION) is not None:
        return [f"{INSTRUCTION_FILE} (update)"]
    return []


def _confirm_yes_no(prompt: str, default_yes: bool = True) -> bool:
    """Prompt the user [Y/n] and return True for yes.

    Non-interactive environments (no TTY on stdin, e.g. an installer or
    extension piping the CLI) return ``default_yes`` without blocking, since
    nobody is there to answer. See: #173, #174
    """
    if not sys.stdin.isatty():
        return default_yes
    suffix = "[Y/n]" if default_yes else "[y/N]"
    try:
        answer = input(f"{prompt} {suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not answer:
        return default_yes
    return answer in ("y", "yes")


def _match_files_to_forget(
    stored_files: Iterable[str],
    patterns: Iterable[str],
    repo_root: Path,
) -> list[str]:
    """Resolve user-supplied paths/globs to stored graph file paths.

    The graph keys every parsed file by its absolute path. A user may name a
    file with an absolute path, a path relative to the repository root, a
    directory whose contents should all be dropped, or a glob pattern. Each
    stored file is compared against every pattern in all of those forms and the
    sorted set of matching stored paths is returned.
    """
    root = repo_root.resolve()
    stored = list(stored_files)
    matched: set[str] = set()

    for raw in patterns:
        pattern = str(raw).strip()
        if not pattern:
            continue
        expanded = Path(pattern).expanduser()
        absolute = expanded if expanded.is_absolute() else root / expanded
        absolute_str = os.path.normpath(str(absolute))
        dir_prefix = absolute_str.rstrip(os.sep) + os.sep

        for stored_path in stored:
            normalised = os.path.normpath(stored_path)
            try:
                relative = os.path.relpath(normalised, str(root))
            except ValueError:
                relative = None

            # Exact match against the absolute or the repo-relative form.
            if normalised == absolute_str:
                matched.add(stored_path)
                continue
            if relative is not None and os.path.normpath(relative) == os.path.normpath(
                pattern
            ):
                matched.add(stored_path)
                continue
            # Every file underneath a named directory.
            if normalised.startswith(dir_prefix):
                matched.add(stored_path)
                continue
            # Glob patterns, matched against both the absolute and relative form.
            if fnmatch.fnmatch(normalised, absolute_str) or (
                relative is not None and fnmatch.fnmatch(relative, pattern)
            ):
                matched.add(stored_path)

    return sorted(matched)


def _report_local_exclude(repo_root: Path, written: list[str]) -> None:
    """Add what install wrote to info/exclude, and say what happened."""
    from .git_exclude import BLOCK_BEGIN, BLOCK_END, NotAGitRepository, exclude_locally

    try:
        result = exclude_locally(repo_root, written)
    except NotAGitRepository:
        print("Not a git repository; nothing added to info/exclude.")
        return
    try:
        shown = str(result.path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        shown = str(result.path)
    if result.malformed:
        print(
            f"{shown} has '{BLOCK_BEGIN}' without '{BLOCK_END}'; left unchanged. "
            "Remove the partial block and reinstall."
        )
    elif result.changed:
        print(f"Excluded locally in {shown}: {', '.join(result.excluded)}")
    elif result.excluded:
        print(f"{shown} already excludes Cartograph's files.")
    if result.tracked:
        # Exclusion has no effect on a tracked file; saying so beats implying
        # these are hidden from git status when they are not.
        print(
            "Already tracked by git, so not excluded (changes to them show in "
            f"git status as usual): {', '.join(result.tracked)}"
        )


def _handle_init(args: argparse.Namespace) -> None:
    """Place the skills pack, hooks and instruction file for GitHub Copilot."""
    from .incremental import find_repo_root

    repo_root = Path(args.repo) if args.repo else find_repo_root()
    if not repo_root:
        repo_root = Path.cwd()

    dry_run = getattr(args, "dry_run", False)
    auto_yes = getattr(args, "yes", False)
    skip_instructions = getattr(args, "no_instructions", False)

    # Preview the instruction files that would be touched (#173).
    instr_targets = _instruction_files_to_modify(repo_root)
    if instr_targets:
        print()
        print("Graph instructions will be injected into:")
        for t in instr_targets:
            print(f"  {t}")

    from .legacy_skills import describe as _describe_legacy
    from .legacy_skills import sweep as _sweep_legacy

    if dry_run:
        if not getattr(args, "no_skills", False):
            for line in _describe_legacy(_sweep_legacy(repo_root, dry_run=True), dry_run=True):
                print(line)
        print("\n[dry-run] Would add the files written to the repository's info/exclude.")
        print("[dry-run] No files were modified.")
        return

    # Repo-relative paths this run wrote, for info/exclude.
    written: list[str] = []

    # Skills and hooks are installed by default so the graph tools are used
    # proactively. Use --no-skills / --no-hooks / --no-instructions to opt out.
    skip_skills = getattr(args, "no_skills", False)
    skip_hooks = getattr(args, "no_hooks", False)
    # Legacy: --skills/--hooks/--all still accepted (no-op, everything is default)

    from .skills import (
        HOST_SKILL_DIR,
        INSTRUCTION_FILE,
        inject_instruction_files,
        install_copilot_hooks,
        install_host_skills,
        skill_documents,
    )

    if not skip_skills:
        print(f"Installed skills in {install_host_skills(repo_root)}")
        # One line per skill rather than the whole directory: a team's own
        # skills in .github/skills must stay visible to git.
        written.extend(f"{HOST_SKILL_DIR}/{slug}/" for slug in skill_documents())
        for line in _describe_legacy(_sweep_legacy(repo_root)):
            print(line)

    # Confirm before writing instruction files (#173). --yes skips the
    # prompt; --no-instructions skips the whole block.
    if not skip_instructions and instr_targets:
        if auto_yes or _confirm_yes_no(
            "Inject graph instructions into the files above?",
            default_yes=True,
        ):
            outcomes = inject_instruction_files(repo_root)
            if outcomes.get(INSTRUCTION_FILE) != "conflict":
                written.append(INSTRUCTION_FILE)
            for label, wording in (
                ("created", "Injected graph instructions into"),
                ("updated", "Updated graph instructions in"),
            ):
                names = [f for f, o in outcomes.items() if o == label]
                if names:
                    print(f"{wording}: {', '.join(names)}")
            # A hand-edited block is never overwritten, so say which file it is
            # rather than reporting success the user did not get (#314).
            stale = [f for f, o in outcomes.items() if o == "conflict"]
            if stale:
                print(
                    "Left edited graph instructions alone in: "
                    f"{', '.join(stale)}. Delete the section between "
                    "<!-- cartograph MCP tools --> and its closing marker "
                    "and reinstall to pick up the current text."
                )
        else:
            print("Skipped instruction injection (user declined).")
    elif skip_instructions:
        print("Skipped instruction injection (--no-instructions).")

    if not skip_hooks:
        hooks_file = install_copilot_hooks(repo_root)
        print(f"Installed Copilot hooks in {hooks_file}")
        written.append(hooks_file.relative_to(repo_root).as_posix())

    _report_local_exclude(repo_root, written)

    print()
    print("Next steps:")
    print("  1. carto build    # build the knowledge graph")
    print("  2. Restart your AI coding tool to pick up the new config")


def _handle_data_dir_option(args, repo_root: Path) -> None:
    """Handle --data-dir option by updating registry if specified."""
    if hasattr(args, "data_dir") and args.data_dir:
        try:
            from .registry import Registry
            data_dir_path = Path(args.data_dir).expanduser().resolve()
            data_dir_path.mkdir(parents=True, exist_ok=True)
            Registry().set_data_dir(str(repo_root), str(data_dir_path))
            logging.info(f"Graph database will be stored at: {data_dir_path}")
        except Exception as exc:
            logging.error(f"Failed to set data directory: {exc}")
            sys.exit(1)


def _add_embedding_refresh_args(command) -> None:
    """Add explicit, provider-scoped refresh options to a CLI command."""
    command.add_argument(
        "--embedding-provider",
        choices=["local", "openai", "google", "minimax", "voyage"],
        default=None,
        help=(
            "Explicitly refresh an existing embedding index with this provider; "
            "requires --embedding-model (default: disabled)"
        ),
    )
    command.add_argument(
        "--embedding-model",
        default=None,
        help=(
            "Exact model for --embedding-provider. Cloud providers may transmit "
            "source-derived text and incur API cost"
        ),
    )


def _embedding_refresh_kwargs(args, parser) -> _EmbeddingRefreshKwargs:
    """Validate the all-or-nothing provider/model opt-in."""
    provider = getattr(args, "embedding_provider", None)
    model = getattr(args, "embedding_model", None)
    if bool(provider) != bool(model):
        parser.error(
            "--embedding-provider and --embedding-model must be supplied together",
        )
    if not provider:
        return {}
    assert isinstance(provider, str)
    assert isinstance(model, str)
    return {
        "embedding_provider": provider,
        "embedding_model": model,
    }


def _non_negative_int(value: str) -> int:
    """Parse a non-negative integer for bounded CLI output."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _positive_int(value: str) -> int:
    """Parse a positive integer for CLI limits."""
    parsed = _non_negative_int(value)
    if parsed == 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _usage_format(parser: argparse.ArgumentParser) -> str:
    """The format a usage error should answer in, before anything is parsed.

    An explicit ``--format``/``--json`` on the command line wins; otherwise the
    parser's own ``--format`` default does, so a malformed call answers in the
    same mode the successful call would have. A command with no ``--format`` at
    all is a human command, and prose is the right answer for it.
    """
    argv = sys.argv[1:]
    for index, token in enumerate(argv):
        if token == "--json":
            return "json"
        if token.startswith("--format="):
            return token.split("=", 1)[1]
        if token == "--format" and index + 1 < len(argv):
            return argv[index + 1]
    for action in parser._actions:  # noqa: SLF001 — argparse exposes no public API
        if action.dest == "output_format":
            return action.default or "text"
    return "text"


class _ContractParser(argparse.ArgumentParser):
    """An ArgumentParser whose usage errors stay inside the capability contract.

    argparse's own ``error()`` exits **2** with prose on stderr and an empty
    stdout. In this protocol exit 2 is PRECONDITION, so an agent reads a
    malformed call as "no graph — run carto build", spends minutes building a
    graph it does not need, retries the identical bad call, and loops. A usage
    error is exit 1 and says so on stdout, in the envelope, where the agent is
    already looking.

    Subparsers inherit this class: ``add_subparsers`` defaults ``parser_class``
    to ``type(self)``, which was verified against this argparse rather than
    assumed, for both the subcommands and ``daemon``'s nested ones.

    ``--help`` and ``--version`` do not come through here — they are actions
    that call ``exit(0)`` directly — so they keep exiting 0 without an envelope.
    """

    def _tool_name(self) -> str:
        # prog is "carto" on the top-level parser and "carto search" on a
        # subparser, so the logical operation is everything after the binary.
        words = self.prog.split()[1:]
        if words:
            return " ".join(words)
        # A top-level error ("unrecognized arguments") still happened during
        # some subcommand, and `tool` is how an agent correlates the failure
        # with the call it made.
        choices: set[str] = set()
        for action in self._actions:  # noqa: SLF001
            if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
                choices = set(action.choices)
        for token in sys.argv[1:]:
            if not token.startswith("-") and token in choices:
                return token
        return self.prog

    def error(self, message: str):  # noqa: D102 — argparse's own contract
        from . import envelope as _env

        env = _env.error(self._tool_name(), _env.Exit.USAGE, message)
        fmt = _usage_format(self)
        if fmt != "json":
            # Text mode keeps argparse's usage block, which is the part a
            # human needs; emit() then writes the message to stderr.
            self.print_usage(sys.stderr)
        raise SystemExit(_env.emit(env, fmt))


_GRAPH_TOOL_COMMANDS = {
    "review-context",
    "review-summary",
    "query",
    "impact",
    "search",
    "flows",
    "flow",
    "communities",
    "community",
    "architecture",
    "large-functions",
    "refactor",
}


_PATH_REPO_COMMANDS = frozenset({
    "install",
    "mem",
    "init",
    "uninstall",
    "build",
    "update",
    "postprocess",
    "embed",
    "watch",
    "status",
    "forget",
    "visualize",
    "wiki",
    "detect-changes",
    "dead-code",
    *_GRAPH_TOOL_COMMANDS,
})


def _canonicalize_repo_argument(args: argparse.Namespace) -> None:
    """Canonicalize path-valued ``--repo`` arguments in place.

    Commands whose ``--repo`` value is a repository *name* rather than a path
    (eval configs and daemon log aliases) are deliberately excluded. Every
    path consumer receives the same absolute, symlink-resolved spelling before
    it opens a database or compares stored paths.
    """
    repo = getattr(args, "repo", None)
    if args.command in _PATH_REPO_COMMANDS and repo:
        args.repo = str(Path(repo).expanduser().resolve())


def _find_explicit_repo_root(start: Path) -> "Path | None":
    """Resolve an explicit --repo for graph-tool commands.

    Walks upward from ``start``, stopping at the nearest directory that
    contains a ``.cartograph``, ``.git``, or ``.svn`` marker. Unlike
    ``find_repo_root``, a registered subproject (``.cartograph``)
    counts as a boundary, so a monorepo subdirectory built with
    ``build --repo mono/module`` resolves to the module — not to the
    monorepo's top-level ``.git`` (#697).
    """
    current = start.resolve()
    if not current.is_dir():
        return None
    while True:
        if any(
            (current / marker).exists()
            for marker in (".cartograph", ".git", ".svn")
        ):
            return current
        if current == current.parent:
            return None
        current = current.parent


def _agent_repo_root(args, command: "str | None" = None) -> Path:
    """Resolve the repository an agent-facing command answers about.

    Shared by the graph tools and `mem`, so a `--repo` one accepts the other
    accepts too and both read the same directory for a given working directory.

    A `--repo` that names no project is exit 1, USAGE: the agent got the call
    wrong, and no remediation Cartograph could run would fix it. It answers in
    the envelope because this is reached *after* parsing, where the promise
    that json mode puts nothing but the envelope on stdout is already in force.
    """
    from . import envelope as _env
    from .incremental import find_project_root

    if not getattr(args, "repo", None):
        return find_project_root()
    # For an explicit --repo the walk must treat .cartograph as a project
    # boundary too: the plain .git/.svn walk resolves a registered monorepo
    # subdirectory to the monorepo root and the graph built at the --repo path
    # is never found (#697). Nearest marker wins, so pointing inside a repo
    # still works.
    repo_root = _find_explicit_repo_root(Path(args.repo).expanduser())
    if repo_root is None:
        message = (
            f"--repo does not look like a project root (no .git, .svn, "
            f"or .cartograph found at or above): {args.repo}"
        )
        env = _env.error(command or args.command, _env.Exit.USAGE, message)
        raise SystemExit(_env.emit(env, getattr(args, "output_format", None) or "text"))
    return repo_root


def _open_page(
    args, repo_root: Path, provenance: "dict | None" = None,
    command: "str | None" = None,
) -> "tuple[int, int | None, str, str]":
    """Settle where this page starts, and widen the fetch to reach it.

    Returns ``(offset, page_limit, query_digest, provenance_digest)`` — what
    ``_emit_tool_result`` needs in order to mint the next cursor. Shared by
    every pageable command, because a second copy would be a second place to
    get the digest-before-widening rule below wrong, and getting it wrong
    produces a cursor that rejects itself on redemption.
    """
    from . import cursor as _cursor
    from . import envelope as _env

    tool = command or args.command

    # The digest binds what the CALLER passed. It is taken before the fetch is
    # widened below, or a cursor minted at --limit 25 would reject itself on
    # redemption against the widened value.
    query = _cursor.query_digest(
        tool, vars(args), extra={"repo_root": str(repo_root)}
    )
    snapshot = _cursor.provenance_digest(provenance)

    offset = 0
    if getattr(args, "cursor", None):
        try:
            offset = _cursor.decode(args.cursor, query=query, provenance=snapshot)
        except _cursor.CursorError as exc:
            raise SystemExit(
                _env.emit(
                    _cursor.rejection(tool, exc),
                    getattr(args, "output_format", None) or "json",
                )
            )

    limit_dest, page_limit = _caller_limit(args)
    if offset and limit_dest and page_limit:
        # Widen the fetch so the requested window is inside it; the head is
        # discarded in _page_for. The tools take no offset of their own.
        setattr(args, limit_dest, page_limit + offset)
    return offset, page_limit, query, snapshot


def _precondition_exit(tool: str, message: str, remediation: str, *, fmt: str) -> None:
    """Refuse a well-formed call whose environment is not ready. Never returns.

    Exit 2, and in json mode an envelope carrying the command that fixes it, so
    the agent self-heals rather than failing the user's task. One helper rather
    than one guard's worth of it per call site: the remediation is the part
    that has to be there, and a copy is where it goes missing.
    """
    from . import envelope as _env

    if fmt == "json":
        env = _env.error(tool, _env.Exit.PRECONDITION, message, remediation=remediation)
        raise SystemExit(_env.emit(env, "json"))
    print(f"{message} Run `{remediation}` first.", file=sys.stderr)
    raise SystemExit(_env.Exit.PRECONDITION)


def _run_graph_tool_command(
    args, repo_root: Path, provenance: "dict | None" = None
) -> None:
    """Run one graph-tool CLI wrapper and emit exactly one JSON value."""
    from . import tools

    root = str(repo_root)
    offset, page_limit, query, snapshot = _open_page(args, repo_root, provenance)
    if args.command in ("review-context", "review-summary"):
        from .review_shape import shape_review_context, shape_review_summary
        from .tools.review import get_review_context

        is_full = args.command == "review-context"
        raw = get_review_context(
            changed_files=args.files,
            repo_root=root,
            base=args.base,
            detail_level="standard" if is_full else "minimal",
            max_depth=getattr(args, "max_depth", 2),
            max_results=getattr(args, "max_results", 50),
            max_files=getattr(args, "max_files", 25),
            include_source=not getattr(args, "no_source", False),
        )
        shaper = shape_review_context if is_full else shape_review_summary
        result = shaper(raw, repo_root=root)
    elif args.command == "query":
        result = tools.query_graph(
            pattern=args.pattern,
            target=args.target,
            repo_root=root,
            # Previously dropped on the floor: the CLI accepted neither, so
            # there was no way to control detail or result count from the
            # command line even though the tool has always supported both.
            detail_level=args.detail_level,
            max_results=args.max_results,
        )
    elif args.command == "impact":
        result = tools.get_impact_radius(
            changed_files=args.files,
            max_depth=args.depth,
            max_results=args.max_results,
            repo_root=root,
            base=args.base,
        )
        see_all = _impact_see_all(args, result, repo_root)
        if see_all:
            result["see_all"] = see_all
    elif args.command == "search":
        result = tools.semantic_search_nodes(
            query=args.query,
            kind=args.kind,
            limit=args.limit,
            repo_root=root,
        )
    elif args.command == "flows":
        result = tools.list_flows(
            repo_root=root,
            sort_by=args.sort,
            limit=args.limit,
            kind=args.kind,
        )
    elif args.command == "flow":
        result = tools.get_flow(
            flow_id=args.id,
            flow_name=args.name,
            include_source=args.source,
            repo_root=root,
        )
    elif args.command == "communities":
        result = tools.list_communities_func(
            repo_root=root,
            sort_by=args.sort,
            min_size=args.min_size,
        )
    elif args.command == "community":
        result = tools.get_community_func(
            community_name=args.name,
            community_id=args.id,
            include_members=args.members,
            repo_root=root,
        )
    elif args.command == "architecture":
        result = tools.get_architecture_overview_func(
            repo_root=root,
            detail_level=args.detail_level,
        )
    elif args.command == "large-functions":
        result = tools.find_large_functions(
            min_lines=args.min_lines,
            kind=args.kind,
            file_path_pattern=args.path,
            limit=args.limit,
            repo_root=root,
            include_generated=args.include_generated,
        )
    else:
        result = tools.refactor_func(
            mode=args.mode,
            old_name=args.old_name,
            new_name=args.new_name,
            kind=args.kind,
            file_pattern=args.path,
            repo_root=root,
            max_results=args.max_results,
        )
    _emit_tool_result(
        args, result,
        offset=offset, page_limit=page_limit,
        query=query, snapshot=snapshot, provenance=provenance,
        repo_root=root,
    )


def _impact_see_all(args, result: dict, repo_root: Path) -> "str | None":
    """The command that lists every impacted item, when this response does not.

    Written out whole rather than described, so an agent that needs the full
    scope runs it instead of reconstructing it. ``--detail full`` because the
    reason to want everything is usually the edges as well.
    """
    import shlex

    from . import repo_paths as _paths

    totals = result.get("totals") or {}
    total = totals.get("items") or 0
    if not result.get("truncated") or not total:
        return None
    files = [
        _paths.relativise(f, repo_root) for f in result.get("changed_files") or []
    ]
    parts = ["carto", "impact", "--files", *files, "--depth", str(args.depth),
             "--limit", str(total), "--detail", "full"]
    if getattr(args, "repo", None):
        parts += ["--repo", args.repo]
    return " ".join(shlex.quote(p) for p in parts)


#: Result keys that are list-shaped and therefore the natural pageable
#: collection for their command. Contract amendment A8 allows at most ONE
#: pageable collection per envelope — a cursor could not be interpreted
#: unambiguously otherwise — so this names, per command, the candidates from
#: which exactly one is chosen at emit time. A command whose result shape
#: depends on its mode (`refactor`) needs the alternatives spelled out; a
#: single-key entry is written as a one-element tuple so there is one rule.
_PAGEABLE_COLLECTION: dict[str, tuple[str, ...]] = {
    # review-context pages `items` (the impacted-node work queue) — the one
    # collection A8 permits. Everything else lives bounded in facets.
    "review-context": ("items",),
    "query": ("results",),
    "impact": ("impacted_nodes",),
    "search": ("results",),
    "flows": ("flows",),
    "communities": ("communities",),
    "large-functions": ("results",),
    # One key per refactor mode: rename returns edits, dead_code returns
    # dead_code, suggest returns suggestions. There is never more than one in a
    # response, so A8 still holds.
    "refactor": ("edits", "dead_code", "suggestions"),
    "dead-code": ("items",),
    # Keyed by the logical operation, not by args.command: all three `mem`
    # subcommands parse as command "mem", and only one of them pages.
    "mem search": ("items",),
}

#: Where a command reports the exact size of its pageable collection. Without
#: one, a page that comes back full is the only hint of more, and a response
#: holding every row at exactly the limit says `has_more` when there is none.
_PAGE_TOTAL: dict[str, tuple[str, ...]] = {
    "impact": ("totals", "items"),
}

#: Where argparse puts a result cap. The flag is spelled `--limit` on some
#: commands and `--max-results` on others, and the dest follows the spelling,
#: so the one thing `page.limit` must report — what the CALLER asked for — is
#: not reliably at any single attribute name.
_LIMIT_DESTS = ("max_results", "limit")


def _caller_limit(args) -> "tuple[str | None, int | None]":
    """The attribute holding the caller's row cap, and its value.

    Returned together because offset paging has to widen the cap in place
    before the tool runs, and report the ORIGINAL in ``page.limit`` after.
    """
    for dest in _LIMIT_DESTS:
        value = getattr(args, dest, None)
        if value:
            return dest, value
    return None, None


def _page_for(
    args, command: str, result: dict, *,
    offset: int = 0, page_limit: "int | None" = None,
) -> "object | None":
    """Describe the one pageable collection, or None when nothing pages.

    A page exists only where a cap does. Falling back to ``len(items)`` made
    ``has_more`` the tautology ``len >= len`` — always true, so an empty result
    advertised a next page — and put ``limit: 0`` on the wire, which the schema
    forbids outright.
    """
    from . import envelope as _env

    if not isinstance(result, dict):
        return None
    key = next(
        (k for k in _PAGEABLE_COLLECTION.get(command, ())
         if isinstance(result.get(k), list)),
        None,
    )
    limit = page_limit if page_limit is not None else _caller_limit(args)[1]
    if key is None or not limit:
        return None
    if offset:
        # The tools have no offset of their own, so the earlier pages' rows
        # were fetched again and are discarded here — the one place that knows
        # which collection pages. Before result_count is taken, or the count
        # would describe rows the caller already has.
        result[key] = result[key][offset:]
    items = result[key]
    total: object = result
    for part in _PAGE_TOTAL.get(command, ()):
        total = total.get(part) if isinstance(total, dict) else None
    if isinstance(total, int) and not isinstance(total, bool):
        has_more = offset + len(items) < total
    else:
        # The tool truncates at the cap, so a full page means there may be
        # more — and anything short of it is the whole answer.
        has_more, total = len(items) >= limit, None
    return _env.Page(
        limit=limit,
        has_more=has_more,
        result_count=len(items),
        total_estimated=total,
        # `collection` names the pageable list only when it is NOT the
        # conventional `data.items`. Emitting "items" would be noise.
        collection=None if key in ("items", "results") else key,
    )


def _emit_tool_result(
    args, result: dict, *,
    command: "str | None" = None,
    offset: int = 0, page_limit: "int | None" = None,
    query: "str | None" = None, snapshot: "str | None" = None,
    provenance: "dict | None" = None,
    repo_root: "str | Path | None" = None,
    search_mode: "str | None" = None,
) -> None:
    """Wrap a tool result in the capability envelope and print it.

    This is the shared emit path for query, impact, search, flows, communities,
    architecture, large-functions, refactor, detect-changes, dead-code and
    `mem`, so the envelope retrofit lands on all of them at once.

    ``command`` overrides the argparse command name, which a nested subcommand
    needs: ``args.command`` is ``mem`` for all three of them, and the operation
    an agent correlates its call with is ``mem search``.

    ``search_mode`` is passed explicitly where the tool keeps it out of
    ``data`` — the store's own vocabulary ("fts") is an implementation name
    that the envelope normalises but that nothing should put in front of an
    agent unnormalised.
    """
    from . import cursor as _cursor
    from . import envelope as _env
    from . import repo_paths as _paths

    fmt = getattr(args, "output_format", None) or "json"
    command = command or args.command

    # The graph stores absolute paths, so every id, name and edge endpoint
    # arrives carrying this checkout's prefix — noise an agent pays for on
    # every row. review-context is already shaped relative; this is the same
    # shortening for the commands that have no shaper of their own. Before
    # paging, so the rows a cursor is minted from are the rows emitted.
    _paths.relativise_result(result, repo_root)
    # The tool reports its own truncation and search mode; surface them rather
    # than inventing either. Read before compacting, which drops the copies in
    # `data` because the envelope carries both.
    truncated = bool(result.get("truncated")) if isinstance(result, dict) else False
    if search_mode is None and isinstance(result, dict):
        search_mode = result.get("search_mode")
    if getattr(args, "detail", None) == "compact":
        # After relativising, so rows are written from the short paths; before
        # paging, so a cursor counts the rows that are emitted.
        from . import compact as _compact

        _compact.compact(command, result)

    page = _page_for(args, command, result, offset=offset, page_limit=page_limit)
    if page is not None and query is not None:
        # Hold the cursor's width before fitting. fit() must budget with it
        # present, because a token stamped afterwards cannot be paid for —
        # see docs/design/token-budget.md.
        _cursor.reserve(page)

    env = _env.ok(
        command,
        data=result,
        provenance=provenance,
        page=page,
        truncated=truncated,
        truncated_reason="page_limit" if truncated else None,
        search_mode=search_mode,
    )
    env = _env.fit(env, getattr(args, "max_tokens", None))
    if page is not None and query is not None:
        # After fitting, so the offset counts what was EMITTED. An offset taken
        # from the fetch would step over the rows the budget dropped and the
        # agent would never learn they existed.
        _cursor.finalise(env, start=offset, query=query, provenance=snapshot)
    # Budget already applied; fitting again could trim below the count the
    # cursor was just minted from.
    raise SystemExit(_env.emit(env, fmt, None))


def _normalise_output_format(args) -> None:
    """Settle ``--format`` once, before anything dispatches on it.

    Two rules every command agrees on: ``--json`` is the deprecated spelling of
    ``--format json``, and a command whose default depends on another flag
    resolves it here rather than at each use site.
    """
    if getattr(args, "json_output", False):
        args.output_format = "json"
    elif getattr(args, "output_format", "") is None:
        # detect-changes: --brief is a rendered panel, so it means text.
        # Without it the command stays machine-first, as it always has been.
        args.output_format = "text" if getattr(args, "brief", False) else "json"


def main() -> None:
    """Main CLI entry point."""
    # Before argparse, because this is not a command and must not appear in any
    # catalogue: it is how a frozen build reaches its own grammar loader, where
    # `sys.executable -c` is not available. See GRAMMAR_PROBE_FLAG.
    if len(sys.argv) == 3 and sys.argv[1] == GRAMMAR_PROBE_FLAG:
        from .parser import run_grammar_probe

        raise SystemExit(run_grammar_probe(sys.argv[2]))

    _configure_utf8_stdio()
    ap = _ContractParser(
        prog="carto",
        description="Persistent incremental knowledge graph for code reviews",
    )
    ap.add_argument("-v", "--version", action="store_true", help="Show version and exit")
    sub = ap.add_subparsers(dest="command")

    # install (primary) + init (alias)
    install_cmd = sub.add_parser(
        "install", help="Place skills, hooks and instructions for AI coding platforms"
    )
    install_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    install_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without writing files",
    )
    install_cmd.add_argument(
        "--no-skills",
        action="store_true",
        help="Skip writing the skills pack to .github/skills/",
    )
    install_cmd.add_argument(
        "--no-hooks",
        action="store_true",
        help="Skip writing .github/hooks/cartograph.json",
    )
    install_cmd.add_argument(
        "--no-instructions",
        action="store_true",
        help="Skip writing .github/instructions/cartograph.instructions.md",
    )
    install_cmd.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Auto-confirm instruction injection without an interactive prompt",
    )
    # Legacy flags (kept for backwards compat, now no-ops since all is default)
    install_cmd.add_argument("--skills", action="store_true", help=argparse.SUPPRESS)
    install_cmd.add_argument("--hooks", action="store_true", help=argparse.SUPPRESS)
    install_cmd.add_argument(
        "--all", action="store_true", dest="install_all", help=argparse.SUPPRESS
    )
    install_cmd.add_argument(
        "--platform",
        choices=_PLATFORM_CHOICES,
        default="copilot",
        help="Target host: copilot, for both Copilot CLI and Copilot Chat (the default)",
    )

    init_cmd = sub.add_parser("init", help="Alias for install")
    init_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    init_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without writing files",
    )
    init_cmd.add_argument(
        "--no-skills",
        action="store_true",
        help="Skip writing the skills pack to .github/skills/",
    )
    init_cmd.add_argument(
        "--no-hooks",
        action="store_true",
        help="Skip writing .github/hooks/cartograph.json",
    )
    init_cmd.add_argument(
        "--no-instructions",
        action="store_true",
        help="Skip writing .github/instructions/cartograph.instructions.md",
    )
    init_cmd.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Auto-confirm instruction injection without an interactive prompt",
    )
    init_cmd.add_argument("--skills", action="store_true", help=argparse.SUPPRESS)
    init_cmd.add_argument("--hooks", action="store_true", help=argparse.SUPPRESS)
    init_cmd.add_argument("--all", action="store_true", dest="install_all", help=argparse.SUPPRESS)
    init_cmd.add_argument(
        "--platform",
        choices=_PLATFORM_CHOICES,
        default="copilot",
        help="Target host: copilot, for both Copilot CLI and Copilot Chat (the default)",
    )

    uninstall_cmd = sub.add_parser(
        "uninstall",
        help="Safely remove carto data, configs, hooks, and generated skills",
    )
    uninstall_cmd.add_argument(
        "--repo",
        default=None,
        help="Path inside a Git/SVN repository to clean (default: current directory)",
    )
    uninstall_cmd.add_argument(
        "--all-repos",
        action="store_true",
        help="Also clean every repository listed in the CRG registry",
    )
    uninstall_cmd.add_argument(
        "--keep-data",
        action="store_true",
        help="Keep graph databases while removing installed integrations",
    )
    uninstall_cmd.add_argument(
        "--keep-user-configs",
        action="store_true",
        help="Clean repositories only; do not edit files under the user home",
    )
    uninstall_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="Print every planned action without writing or deleting anything",
    )
    uninstall_cmd.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Apply without an interactive confirmation",
    )

    # build
    build_cmd = sub.add_parser("build", help="Full graph build (re-parse all files)")
    build_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    build_cmd.add_argument("-q", "--quiet", action="store_true", help="Suppress output")
    build_cmd.add_argument(
        "--skip-flows",
        action="store_true",
        help="Skip flow/community detection (signatures + FTS only)",
    )
    build_cmd.add_argument(
        "--skip-postprocess",
        action="store_true",
        help="Skip all post-processing (raw parse only)",
    )
    build_cmd.add_argument(
        "--data-dir",
        default=None,
        help="External directory to store graph database (useful for network shares)"
    )
    _add_embedding_refresh_args(build_cmd)

    # update
    update_cmd = sub.add_parser("update", help="Incremental update (only changed files)")
    update_cmd.add_argument(
        "--base",
        default=None,
        help="Git diff base (default: the commit the graph was last built at)",
    )
    update_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    update_cmd.add_argument("-q", "--quiet", action="store_true", help="Suppress output")
    update_cmd.add_argument(
        "--skip-flows",
        action="store_true",
        help="Skip flow/community detection (signatures + FTS only)",
    )
    update_cmd.add_argument(
        "--skip-postprocess",
        action="store_true",
        help="Skip all post-processing (raw parse only)",
    )
    update_cmd.add_argument(
        "--brief",
        action="store_true",
        help="After re-parsing changed files into the graph, also print the "
             "risk summary + Token Savings panel that 'detect-changes --brief' "
             "prints. Use this after a rebase or large change set when you "
             "want to refresh the graph AND see the impact in one command; "
             "use 'detect-changes --brief' alone when the graph is already "
             "up to date (analysis only, no re-parse).",
    )
    update_cmd.add_argument(
        "--verify",
        action="store_true",
        help="Calibrate the estimated savings against tiktoken's "
             "cl100k_base tokenizer (the GPT-4 family tokenizer). Adds a "
             "second row to the panel with the real token counts. Requires "
             "`pip install tiktoken`.",
    )
    update_cmd.add_argument(
        "--data-dir",
        default=None,
        help="External directory to store graph database (useful for network shares)"
    )
    _add_embedding_refresh_args(update_cmd)

    # postprocess
    pp_cmd = sub.add_parser(
        "postprocess",
        help="Run post-processing on existing graph (flows, communities, FTS)",
    )
    pp_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    pp_cmd.add_argument("--no-flows", action="store_true", help="Skip flow detection")
    pp_cmd.add_argument("--no-communities", action="store_true", help="Skip community detection")
    pp_cmd.add_argument("--no-fts", action="store_true", help="Skip FTS rebuild")
    pp_cmd.add_argument(
        "--data-dir",
        default=None,
        help="External directory to store graph database (useful for network shares)"
    )
    _add_embedding_refresh_args(pp_cmd)

    # embed
    embed_cmd = sub.add_parser(
        "embed",
        help="Compute vector embeddings for semantic search",
    )
    embed_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    embed_cmd.add_argument(
        "--provider",
        choices=["local", "openai", "google", "minimax", "voyage"],
        default=None,
        help="Embedding provider (default: local, needs cartograph[embeddings])",
    )
    embed_cmd.add_argument(
        "--model",
        default=None,
        help="Embedding model. For local: HuggingFace ID (default all-MiniLM-L6-v2); "
             "for openai/google/minimax/voyage: provider-specific model ID.",
    )
    embed_cmd.add_argument(
        "--data-dir",
        default=None,
        help="External directory to store graph database (useful for network shares)"
    )

    # watch
    watch_cmd = sub.add_parser("watch", help="Watch for changes and auto-update")
    watch_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    watch_cmd.add_argument(
        "--data-dir",
        default=None,
        help="External directory to store graph database (useful for network shares)"
    )
    _add_embedding_refresh_args(watch_cmd)

    # status
    status_cmd = sub.add_parser("status", help="Show graph statistics")
    status_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    status_cmd.add_argument("-q", "--quiet", action="store_true", help="Suppress output")
    status_cmd.add_argument(
        "--format",
        choices=["json", "text"],
        default="text",
        dest="output_format",
        help="Output format; 'json' emits the Cartograph capability envelope",
    )
    status_cmd.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        dest="max_tokens",
        help="Token budget for the response",
    )
    status_cmd.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Deprecated alias for --format json (kept so existing hooks keep working)",
    )
    status_cmd.add_argument(
        "--data-dir",
        default=None,
        help="External directory to store graph database (useful for network shares)"
    )

    # forget
    forget_cmd = sub.add_parser(
        "forget",
        help="Remove already-parsed files from the graph without a full rebuild",
    )
    forget_cmd.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="Files, directories, or glob patterns to drop from the graph. "
             "Paths may be absolute or relative to the repository root.",
    )
    forget_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    forget_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="List the files that would be forgotten without modifying the graph",
    )
    forget_cmd.add_argument(
        "--data-dir",
        default=None,
        help="External directory to store graph database (useful for network shares)"
    )

    # visualize
    vis_cmd = sub.add_parser("visualize", help="Generate interactive HTML graph visualization")
    vis_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    vis_cmd.add_argument(
        "--mode",
        choices=["auto", "full", "community", "file"],
        default="auto",
        help="Rendering mode: auto (default), full, community, or file",
    )
    vis_cmd.add_argument(
        "--serve",
        action="store_true",
        help="Start a local HTTP server to view the visualization (localhost:8765)",
    )
    vis_cmd.add_argument(
        "--format",
        choices=["html", "json", "graphml", "cypher", "obsidian", "svg"],
        default="html",
        help="Export format (default: html)",
    )
    vis_cmd.add_argument(
        "--data-dir",
        default=None,
        help="External directory to store graph database (useful for network shares)"
    )

    # wiki
    wiki_cmd = sub.add_parser("wiki", help="Generate markdown wiki from community structure")
    wiki_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    wiki_cmd.add_argument(
        "--force",
        action="store_true",
        help="Regenerate all pages even if content unchanged",
    )
    wiki_cmd.add_argument(
        "--data-dir",
        default=None,
        help="External directory to store graph database (useful for network shares)"
    )

    # register
    register_cmd = sub.add_parser(
        "register", help="Register a repository in the multi-repo registry"
    )
    register_cmd.add_argument("path", help="Path to the repository root")
    register_cmd.add_argument("--alias", default=None, help="Short alias for the repository")

    # unregister
    unregister_cmd = sub.add_parser(
        "unregister", help="Remove a repository from the multi-repo registry"
    )
    unregister_cmd.add_argument("path_or_alias", help="Repository path or alias to remove")

    # repos
    sub.add_parser("repos", help="List registered repositories")

    # eval
    eval_cmd = sub.add_parser("eval", help="Run evaluation benchmarks")
    eval_cmd.add_argument(
        "--benchmark",
        default=None,
        help="Comma-separated benchmarks to run (token_efficiency, impact_accuracy, "
        "agent_baseline, flow_completeness, search_quality, build_performance, "
        "multi_hop_retrieval)",
    )
    eval_cmd.add_argument("--repo", default=None, help="Comma-separated repo config names")
    eval_cmd.add_argument("--all", action="store_true", dest="run_all", help="Run all benchmarks")
    eval_cmd.add_argument("--report", action="store_true", help="Generate report from results")
    eval_cmd.add_argument("--output-dir", default=None, help="Output directory for results")
    eval_cmd.add_argument(
        "--embed",
        action="store_true",
        help=(
            "Build the vector index after each graph build. Required by the "
            "agent_baseline, search_quality and multi_hop_retrieval "
            "benchmarks: without it their natural-language questions hit "
            "FTS5 only and return zero results (default: disabled)"
        ),
    )
    eval_cmd.add_argument(
        "--embed-provider",
        choices=["local", "openai", "google", "minimax", "voyage"],
        default=None,
        help="Provider for --embed (default: local, needs "
             "cartograph[embeddings])",
    )
    eval_cmd.add_argument(
        "--embed-model",
        default=None,
        help="Model for --embed (default: the provider's own default)",
    )

    # detect-changes
    detect_cmd = sub.add_parser(
        "detect-changes",
        help="Analyze change impact against the existing graph (read-only). "
             "Does NOT re-parse files — for that, use 'update --brief'.",
    )
    detect_cmd.add_argument("--base", default="HEAD~1", help="Git diff base (default: HEAD~1)")
    detect_cmd.add_argument(
        "--brief",
        action="store_true",
        help="Show the risk summary + Token Savings panel instead of the "
             "full JSON. Read-only against the existing graph.",
    )
    detect_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    detect_cmd.add_argument(
        "--churn",
        action="store_true",
        help="Add an opt-in change-frequency term to risk scores. Counts "
             "commits per file over 90 days by default; set "
             "CRG_CHURN_WINDOW_DAYS to adjust.",
    )
    detect_cmd.add_argument(
        "--verify",
        action="store_true",
        help="Calibrate the estimated savings against tiktoken's "
             "cl100k_base tokenizer (the GPT-4 family tokenizer). Adds a "
             "second row to the panel with the real token counts. Requires "
             "`pip install tiktoken`.",
    )
    detect_cmd.add_argument(
        "--format",
        choices=["json", "text"],
        # Unset rather than json, because --brief renders a panel and so
        # implies text. Resolved by _normalise_output_format, which is the one
        # place that rule lives.
        default=None,
        dest="output_format",
        help="Output format; defaults to json, or text with --brief",
    )
    detect_cmd.add_argument(
        "--max-tokens", type=int, default=None, dest="max_tokens",
        help="Token budget for the response",
    )
    detect_cmd.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Deprecated alias for --format json",
    )

    hook_cmd = sub.add_parser(
        "hook",
        help="Host-invoked hook entry point (hosts call this; agents must not)",
    )
    hook_cmd.add_argument(
        "event",
        help=(
            "Hook event: session-status, file-update, prompt-capture, "
            "session-summarise or session-catchup (host spellings accepted)"
        ),
    )
    hook_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    hook_cmd.add_argument(
        "--host",
        default=None,
        help=(
            "Which host is calling, e.g. copilot; 'copilot' is narrowed to "
            "copilot-cli or copilot-chat from the environment. Recorded as an "
            "observation's platform_source; only the generated host config "
            "knows it, so nothing downstream has to infer it."
        ),
    )

    # dead-code
    dead_cmd = sub.add_parser(
        "dead-code",
        help="Find functions/classes with no callers or test references",
    )
    dead_cmd.add_argument(
        "--kind",
        choices=["Function", "Class"],
        default=None,
        help="Filter by node kind",
    )
    dead_cmd.add_argument(
        "--file-pattern",
        default=None,
        help="Filter by file path substring",
    )
    dead_cmd.add_argument(
        "--limit",
        type=_non_negative_int,
        default=0,
        help="Maximum rows to print (0 = no limit)",
    )
    dead_cmd.add_argument(
        "--format",
        choices=["json", "text"],
        default="text",
        dest="output_format",
        help="Output format; 'json' emits the Cartograph capability envelope",
    )
    dead_cmd.add_argument(
        "--max-tokens", type=int, default=None, dest="max_tokens",
        help="Token budget for the response",
    )
    dead_cmd.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Deprecated alias for --format json (kept so existing callers keep working)",
    )
    dead_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    dead_cmd.add_argument(
        "--data-dir",
        default=None,
        help="External directory containing the graph database",
    )

    # mem — the memory capability. Its own module owns the parser, so the
    # subcommands, their flags and the code that dispatches them cannot drift.
    from .mem.cli import add_parser as _add_mem_parser

    _add_mem_parser(sub)

    # capabilities — the machine-readable catalogue. Agents that skipped or
    # lack the skills pack discover the surface through this.
    caps_cmd = sub.add_parser(
        "capabilities",
        help="Machine-readable catalogue of commands, flags and examples",
    )
    caps_cmd.add_argument(
        "--command",
        default=None,
        dest="command_name",
        help="Full argument and flag detail for one command",
    )
    caps_cmd.add_argument(
        "--format",
        choices=["json", "text"],
        default="json",
        dest="output_format",
        help="Output format (defaults to json — this command is agent-facing)",
    )
    caps_cmd.add_argument(
        "--max-tokens", type=int, default=None, dest="max_tokens",
        help="Token budget for the response",
    )

    # review-context / review-summary — the headline capability, split into two
    # commands because `minimal` is a different typed response, not less of the
    # same one. See docs/design/review-context-shape.md.
    rc_cmd = sub.add_parser(
        "review-context",
        help="Token-efficient review context for a change set",
    )
    rs_cmd = sub.add_parser(
        "review-summary",
        help="Cheap risk-and-counts summary of a change set",
    )
    for _r in (rc_cmd, rs_cmd):
        _r.add_argument("--base", default="HEAD~1", help="Git diff base (default: HEAD~1)")
        _r.add_argument("--files", nargs="*", default=None, help="Explicit changed files")
        _r.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    rc_cmd.add_argument("--depth", type=int, default=2, dest="max_depth",
                        help="Impact traversal depth")
    rc_cmd.add_argument("--limit", type=int, default=25, dest="max_results",
                        help="Maximum impacted nodes returned (the pageable collection)")
    rc_cmd.add_argument("--max-files", type=int, default=25, dest="max_files",
                        help="Maximum changed files described")
    rc_cmd.add_argument("--no-source", action="store_true", dest="no_source",
                        help="Omit source snippets")

    # Graph tool wrappers
    query_cmd = sub.add_parser("query", help="Query graph relationships")
    query_cmd.add_argument(
        "pattern",
        # All 16 patterns dispatched by tools.query_graph. The CLI previously
        # hard-coded only 8 of them here, making half the query surface
        # unreachable from the command line even though the engine supported it.
        choices=QUERY_PATTERNS,
        metavar="PATTERN",
        help="One of: " + ", ".join(QUERY_PATTERNS),
    )
    query_cmd.add_argument("target", help="Node name, qualified name, or file path")
    query_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")
    query_cmd.add_argument(
        "--detail-level",
        choices=["minimal", "standard", "full"],
        default="standard",
        dest="detail_level",
        help="How much detail per result",
    )
    query_cmd.add_argument(
        "--limit",
        type=int,
        default=100,
        dest="max_results",
        help="Maximum results to return",
    )

    impact_cmd = sub.add_parser("impact", help="Analyze the blast radius of changes")
    impact_cmd.add_argument(
        "--files",
        nargs="+",
        default=None,
        help="Changed files (auto-detected when omitted)",
    )
    impact_cmd.add_argument("--depth", type=_non_negative_int, default=2)
    # 20, not the 500 it was: every connecting edge of 500 nodes came to ~126k
    # tokens on a 991-file repository. What the cap leaves out is still counted
    # — see docs/design/compact-output.md, "impact". Spelled --limit, as on
    # every other list command; it was --max-results.
    impact_cmd.add_argument(
        "--limit", type=_positive_int, default=20,
        dest="max_results",
        help="Most affected items listed, direct dependents first (the pageable "
             "collection). Totals and the file list always cover every item",
    )
    impact_cmd.add_argument("--base", default="HEAD~1")
    impact_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    search_cmd = sub.add_parser("search", help="Search graph entities")
    search_cmd.add_argument("query", help="Search string")
    search_cmd.add_argument(
        "--kind",
        choices=["File", "Class", "Function", "Type", "Test"],
        default=None,
    )
    search_cmd.add_argument("--limit", type=_positive_int, default=20)
    search_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    flows_cmd = sub.add_parser("flows", help="List stored execution flows")
    flows_cmd.add_argument(
        "--sort",
        choices=["criticality", "depth", "node_count", "file_count", "name"],
        default="criticality",
    )
    flows_cmd.add_argument("--limit", type=_positive_int, default=50)
    flows_cmd.add_argument("--kind", default=None, help="Entry-point kind filter")
    flows_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    flow_cmd = sub.add_parser("flow", help="Show one stored execution flow")
    flow_selector = flow_cmd.add_mutually_exclusive_group(required=True)
    flow_selector.add_argument("--id", type=_positive_int, default=None)
    flow_selector.add_argument("--name", default=None)
    flow_cmd.add_argument("--source", action="store_true", help="Include source snippets")
    flow_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    communities_cmd = sub.add_parser("communities", help="List graph communities")
    communities_cmd.add_argument(
        "--sort",
        choices=["size", "cohesion", "name"],
        default="size",
    )
    communities_cmd.add_argument("--min-size", type=_non_negative_int, default=0)
    communities_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    community_cmd = sub.add_parser("community", help="Show one graph community")
    community_selector = community_cmd.add_mutually_exclusive_group(required=True)
    community_selector.add_argument("--id", type=_positive_int, default=None)
    community_selector.add_argument("--name", default=None)
    community_cmd.add_argument("--members", action="store_true")
    community_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    architecture_cmd = sub.add_parser("architecture", help="Show architecture overview")
    architecture_cmd.add_argument(
        "--detail-level",
        choices=["minimal", "standard"],
        default="minimal",
    )
    architecture_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    large_cmd = sub.add_parser(
        "large-functions", help="Find oversized functions and methods",
    )
    large_cmd.add_argument("--min-lines", type=_positive_int, default=50)
    large_cmd.add_argument(
        "--kind",
        choices=["Function", "Class", "File", "Test", "Type"],
        # Repeated rather than comma-joined, so each value is checked against
        # the choices by argparse and by the skills checker alike. No list
        # default: argparse appends to a mutable default instead of replacing it.
        action="append",
        default=None,
        help="Kind to rank; repeat to widen (default: Function, methods included)",
    )
    large_cmd.add_argument(
        "--include-generated",
        action="store_true",
        dest="include_generated",
        help="Also rank generated, vendored and declaration (.d.ts) files",
    )
    large_cmd.add_argument("--path", default=None, help="File-path substring filter")
    large_cmd.add_argument("--limit", type=_positive_int, default=50)
    large_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    refactor_cmd = sub.add_parser("refactor", help="Preview graph-backed refactors")
    refactor_cmd.add_argument("mode", choices=["rename", "dead_code", "suggest"])
    refactor_cmd.add_argument("--old-name", default=None)
    refactor_cmd.add_argument("--new-name", default=None)
    refactor_cmd.add_argument(
        "--kind",
        choices=["Function", "Class"],
        default=None,
    )
    refactor_cmd.add_argument("--path", default=None, help="File-path substring filter")
    # The tool has always bounded its response; the CLI dropped the control on
    # the floor, which left the caller with a page nobody asked for and no way
    # to say how big it should be. Default matches refactor_func's own.
    refactor_cmd.add_argument(
        "--limit", type=_positive_int, default=50, dest="max_results",
        help="Maximum edits/symbols/suggestions returned (the pageable collection)",
    )
    refactor_cmd.add_argument("--repo", default=None, help="Repository root (auto-detected)")

    # Every graph-tool command shares one emit path (_emit_tool_result), so the
    # contract flags are attached in one place rather than repeated ten times.
    # Only commands with a pageable collection can honour a cursor.
    # `carto capabilities` is generated from this parser, so an unpageable
    # command advertising --cursor would make the catalogue lie.
    for _paged_cmd in (
        rc_cmd, query_cmd, impact_cmd, search_cmd,
        flows_cmd, communities_cmd, large_cmd, refactor_cmd,
    ):
        _paged_cmd.add_argument(
            "--cursor",
            default=None,
            help="Continue from a previous page (opaque; pass page.next_cursor verbatim)",
        )

    for _graph_cmd in (
        query_cmd, impact_cmd, search_cmd, flows_cmd, flow_cmd,
        communities_cmd, community_cmd, architecture_cmd, large_cmd, refactor_cmd,
        rc_cmd, rs_cmd,
    ):
        _graph_cmd.add_argument(
            "--format",
            choices=["json", "text"],
            default="json",
            dest="output_format",
            help="Output format (these commands default to json — they are agent-facing)",
        )
        _graph_cmd.add_argument(
            "--max-tokens",
            type=int,
            default=None,
            dest="max_tokens",
            help="Token budget for the response",
        )

    # One flag, attached from the list compact.py owns, so the commands that
    # take it and the commands it does something for cannot drift apart.
    from . import compact as _compact

    for _name in sorted(_compact.COMMANDS):
        sub.choices[_name].add_argument(
            "--detail",
            choices=_compact.DETAIL_CHOICES,
            default=_compact.DEFAULT_DETAIL,
            help="Row shape: compact (one line per row) or full (every field)",
        )

    # daemon
    daemon_cmd = sub.add_parser(
        "daemon",
        help="Multi-repo watch daemon (start/stop/status/add/remove)",
    )
    daemon_sub = daemon_cmd.add_subparsers(dest="daemon_command")

    daemon_start = daemon_sub.add_parser(
        "start",
        help="Start the watch daemon",
    )
    daemon_start.add_argument(
        "--foreground",
        action="store_true",
        help="Run in foreground instead of daemonizing",
    )

    daemon_sub.add_parser(
        "stop",
        help="Stop the watch daemon",
    )

    daemon_restart = daemon_sub.add_parser(
        "restart",
        help="Restart the watch daemon",
    )
    daemon_restart.add_argument(
        "--foreground",
        action="store_true",
        help="Run in foreground instead of daemonizing",
    )

    daemon_sub.add_parser("status", help="Show daemon and watcher status")

    daemon_logs = daemon_sub.add_parser(
        "logs",
        help="View daemon or watcher logs",
    )
    daemon_logs.add_argument(
        "--repo",
        default=None,
        help="Show logs for a specific repo alias",
    )
    daemon_logs.add_argument(
        "--follow",
        action="store_true",
        help="Follow log output (tail -f)",
    )
    daemon_logs.add_argument(
        "--lines",
        type=int,
        default=50,
        help="Number of lines to show (default: 50)",
    )

    daemon_add = daemon_sub.add_parser(
        "add",
        help="Add a repo to the watch config",
    )
    daemon_add.add_argument("path", help="Path to the repository")
    daemon_add.add_argument(
        "--alias",
        default=None,
        help="Short alias for the repo",
    )

    daemon_remove = daemon_sub.add_parser(
        "remove",
        help="Remove a repo from the watch config",
    )
    daemon_remove.add_argument(
        "path_or_alias",
        help="Repository path or alias to remove",
    )

    args = ap.parse_args()

    if args.version:
        from .release import version_line

        print(version_line())
        return

    _normalise_output_format(args)

    if args.command == "capabilities":
        # Dispatched here, before repo resolution and any database access:
        # capabilities must answer on a machine with no graph, which is exactly
        # when an agent most needs to know what it can run.
        from . import capabilities as _caps
        from . import envelope as _env

        try:
            catalogue = _caps.build_catalogue(
                ap, command=args.command_name, version=_get_version()
            )
        except KeyError as exc:
            raise SystemExit(
                _env.emit(
                    _env.error("capabilities", _env.Exit.USAGE, str(exc.args[0])),
                    args.output_format,
                )
            )
        env = _env.ok("capabilities", data=catalogue)
        raise SystemExit(
            _env.emit(env, args.output_format, getattr(args, "max_tokens", None))
        )

    if args.command == "hook":
        # Dispatched here, ahead of repo resolution and every data-dir
        # side effect below it. A hook runs on someone else's schedule, on a
        # repository that may have no graph at all, and the query path's
        # machinery — envelopes, exit codes, directory creation — is the wrong
        # protocol for it. See cartograph.hook.
        from .hook import run as _run_hook

        raise SystemExit(_run_hook(args.event, repo=args.repo, host=args.host))

    if not args.command:
        _print_banner()
        return

    _canonicalize_repo_argument(args)

    if (
        args.command == "refactor"
        and args.mode == "rename"
        and (not args.old_name or not args.new_name)
    ):
        refactor_cmd.error("rename requires --old-name and --new-name")

    if args.command == "mem":
        # Ahead of the graph-tool block and independent of it: observations
        # outlive any particular build, so `carto mem` must answer on a
        # repository whose graph has never been built.
        from .mem.cli import run as _run_mem

        if os.environ.get("CARTO_DEBUG"):
            # Otherwise unconfigured on purpose: Python's last-resort handler
            # prints warnings as bare lines, which is all an agent should read.
            logging.basicConfig(level=logging.DEBUG, format="%(levelname)s: %(message)s",
                                stream=sys.stderr)
        command = f"mem {args.mem_command}"
        _run_mem(args, _agent_repo_root(args, command))
        return

    if args.command in _GRAPH_TOOL_COMMANDS:
        from .incremental import get_db_path

        repo_root = _agent_repo_root(args)
        db_path = get_db_path(repo_root)
        if not db_path.exists():
            # The graph-tool path (query, impact, search, flows, …), which is
            # the one agents actually hit most often.
            _precondition_exit(
                args.command,
                f"No graph found at {db_path}.",
                "carto build",
                fmt=getattr(args, "output_format", "json"),
            )
        from .graph import GraphStore as _GraphStore

        # Graph-tool responses carry no provenance today. Cursors bind to it,
        # and without it an agent cannot tell that two pages came from two
        # different builds — which is the whole failure being designed out.
        with _GraphStore(db_path) as _store:
            provenance = {
                "graph_sha": _store.get_metadata("git_head_sha"),
                "built_at": _store.get_metadata("last_updated"),
            }
        _run_graph_tool_command(args, repo_root, provenance)
        return

    embedding_refresh_kwargs = _embedding_refresh_kwargs(args, ap)

    if args.command == "daemon":
        if not args.daemon_command:
            daemon_cmd.print_help()
            return
        from .daemon_cli import (
            _handle_add,
            _handle_logs,
            _handle_remove,
            _handle_restart,
            _handle_start,
            _handle_status,
            _handle_stop,
        )

        handlers = {
            "start": _handle_start,
            "stop": _handle_stop,
            "restart": _handle_restart,
            "status": _handle_status,
            "logs": _handle_logs,
            "add": _handle_add,
            "remove": _handle_remove,
        }
        handler = handlers.get(args.daemon_command)
        if handler:
            handler(args)
        return

    if args.command == "eval":
        from .eval.reporter import generate_full_report, generate_readme_tables
        from .eval.runner import run_eval

        if getattr(args, "report", False):
            output_dir = Path(getattr(args, "output_dir", None) or "evaluate/results")
            report = generate_full_report(output_dir)
            report_path = Path("evaluate/reports/summary.md")
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(report, encoding="utf-8")
            print(f"Report written to {report_path}")

            tables = generate_readme_tables(output_dir)
            print("\n--- README Tables (copy-paste) ---\n")
            print(tables)
        else:
            repos = (
                [r.strip() for r in args.repo.split(",")] if getattr(args, "repo", None) else None
            )
            benchmarks = (
                [b.strip() for b in args.benchmark.split(",")]
                if getattr(args, "benchmark", None)
                else None
            )

            if not repos and not benchmarks and not getattr(args, "run_all", False):
                print("Specify --all, --repo, or --benchmark. See --help.")
                return

            results = run_eval(
                repos=repos,
                benchmarks=benchmarks,
                output_dir=getattr(args, "output_dir", None),
                embed=getattr(args, "embed", False),
                embedding_provider=getattr(args, "embed_provider", None),
                embedding_model=getattr(args, "embed_model", None),
            )
            print(f"\nCompleted {len(results)} benchmark(s).")
            print("Run 'carto eval --report' to generate tables.")
        return

    if args.command == "uninstall":
        from .uninstall import UninstallReport
        from .uninstall import run as run_uninstall

        target_repo = Path(args.repo).expanduser() if args.repo else None
        options = {
            "repo": target_repo,
            "all_repos": args.all_repos,
            "keep_data": args.keep_data,
            "keep_user_configs": args.keep_user_configs,
        }

        def _print_report(report: UninstallReport) -> None:
            for action in report.removed_paths:
                print(f"  delete  {action}")
            for action in report.edited_paths:
                print(f"  edit    {action}")
            for action in report.skipped_paths:
                print(f"  skip    {action}")
            for error in report.errors:
                print(f"  error   {error}")

        preview = run_uninstall(**options, dry_run=True)
        print("carto uninstall — planned actions:")
        _print_report(preview)
        if preview.total_actions == 0:
            if preview.errors:
                raise SystemExit(1)
            print("  (nothing to do — no carto artifacts found)")
            return
        if args.dry_run:
            print("\n[dry-run] No changes made.")
            if preview.errors:
                raise SystemExit(1)
            return
        if not args.yes and not _confirm_yes_no(
            "\nProceed with uninstall?", default_yes=False
        ):
            print("Aborted.")
            return

        uninstall_result = run_uninstall(**options, dry_run=False)
        print("\nApplied actions:")
        _print_report(uninstall_result)
        print(
            f"Done. Removed {len(uninstall_result.removed_paths)} path(s); "
            f"edited {len(uninstall_result.edited_paths)} shared file(s)."
        )
        if uninstall_result.errors:
            raise SystemExit(1)
        return

    if args.command in ("init", "install"):
        _handle_init(args)
        return

    if args.command in ("register", "unregister", "repos"):
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
        from .registry import Registry

        registry = Registry()
        if args.command == "register":
            try:
                entry = registry.register(args.path, alias=args.alias)
                alias_info = f" (alias: {entry['alias']})" if entry.get("alias") else ""
                print(f"Registered: {entry['path']}{alias_info}")
            except ValueError as exc:
                logging.error(str(exc))
                sys.exit(1)
        elif args.command == "unregister":
            if registry.unregister(args.path_or_alias):
                print(f"Unregistered: {args.path_or_alias}")
            else:
                print(f"Not found: {args.path_or_alias}")
                sys.exit(1)
        elif args.command == "repos":
            repos = registry.list_repos()
            if not repos:
                print("No repositories registered.")
                print("Use: carto register <path> [--alias name]")
            else:
                for entry in repos:
                    alias = entry.get("alias", "")
                    alias_str = f"  ({alias})" if alias else ""
                    print(f"  {entry['path']}{alias_str}")
        return

    # stream is named rather than left to the default: json mode promises that
    # stdout carries nothing but the envelope, and a log line landing there
    # hands the agent a JSONDecodeError instead of an answer.
    # CARTO_DEBUG=1 is where the detail behind a one-line warning goes: the
    # full traceback of an embedding runtime that would not load, for one.
    logging.basicConfig(
        level=logging.DEBUG if os.environ.get("CARTO_DEBUG") else logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    from .graph import GraphStore
    from .incremental import (
        LEGACY_DB_FILE,
        find_project_root,
        find_repo_root,
        get_db_path,
        watch,
    )

    if args.command == "postprocess":
        repo_root = Path(args.repo) if args.repo else find_project_root()
        _handle_data_dir_option(args, repo_root)
        db_path = get_db_path(repo_root)
        store = GraphStore(db_path)
        try:
            from .tools.build import run_postprocess

            result = run_postprocess(
                flows=not getattr(args, "no_flows", False),
                communities=not getattr(args, "no_communities", False),
                fts=not getattr(args, "no_fts", False),
                repo_root=str(repo_root),
                **embedding_refresh_kwargs,
            )
            parts = []
            if result.get("flows_detected"):
                parts.append(f"{result['flows_detected']} flows")
            if result.get("communities_detected"):
                parts.append(f"{result['communities_detected']} communities")
            if result.get("fts_indexed"):
                parts.append(f"{result['fts_indexed']} FTS entries")
            print(f"Post-processing: {', '.join(parts) or 'done'}")
        finally:
            store.close()
        return

    if args.command == "embed":
        repo_root = Path(args.repo) if args.repo else find_project_root()
        _handle_data_dir_option(args, repo_root)
        from .tools.docs import embed_graph

        result = embed_graph(
            repo_root=str(repo_root),
            model=args.model,
            provider=args.provider,
        )
        if result.get("status") == "error":
            logging.error(result.get("error", "embed_graph failed"))
            sys.exit(1)
        print(result.get("summary", "Embedding done."))
        return

    if args.command in ("update", "detect-changes"):
        # update and detect-changes require git for diffing
        repo_root = Path(args.repo) if args.repo else find_repo_root()
        if not repo_root:
            logging.error(
                "Not in a git repository. '%s' requires git for diffing.",
                args.command,
            )
            logging.error("Use 'build' for a full parse, or run 'git init' first.")
            sys.exit(1)
    elif args.command == "dead-code":
        requested_root = Path(args.repo).expanduser() if args.repo else None
        repo_root = find_project_root(requested_root)
    else:
        repo_root = Path(args.repo) if args.repo else find_project_root()

    # Handle --data-dir for commands that support it
    _data_dir_cmds = (
        "build",
        "update",
        "detect-changes",
        "status",
        "forget",
        "watch",
        "visualize",
        "wiki",
        "dead-code",
    )
    # Read-only consumers must not create graph.db / data dirs / registry
    # entries when the graph is missing (follow-up to #777 / #782; see #803).
    _read_only_db_cmds = frozenset({
        "status",
        "detect-changes",
        "visualize",
        "wiki",
        "watch",
    })
    explicit_data_dir = bool(getattr(args, "data_dir", None))
    read_only_explicit_data_dir = (
        args.command in _read_only_db_cmds and explicit_data_dir
    )
    if args.command in _data_dir_cmds and not read_only_explicit_data_dir:
        _handle_data_dir_option(args, repo_root)

    if args.command in _read_only_db_cmds:
        if read_only_explicit_data_dir:
            db_path = Path(args.data_dir).expanduser().resolve() / "graph.db"
        else:
            db_path = get_db_path(repo_root, read_only=True)
        legacy_db = repo_root / LEGACY_DB_FILE
        default_db = repo_root / ".cartograph" / "graph.db"
        if (
            not read_only_explicit_data_dir
            and not db_path.exists()
            and db_path.resolve() == default_db.resolve()
            and legacy_db.exists()
        ):
            # Preserve the established one-time legacy migration, but do not
            # materialize graph state when neither database exists.
            db_path = get_db_path(repo_root)
    else:
        db_path = get_db_path(repo_root)
    if (
        args.command in ("dead-code", "forget", *_read_only_db_cmds)
        and not db_path.exists()
    ):
        # A missing graph is a PRECONDITION failure (exit 2), not a usage error
        # (exit 1): the agent called correctly, the environment is not ready.
        want_json = (
            getattr(args, "json_output", False)
            or getattr(args, "output_format", "text") == "json"
        )
        _precondition_exit(
            args.command,
            f"No graph found at {db_path}.",
            "carto build",
            fmt="json" if want_json else "text",
        )
    store = GraphStore(db_path)

    try:
        if args.command == "dead-code":
            from .refactor import find_dead_code

            items = find_dead_code(
                store,
                kind=args.kind,
                file_pattern=args.file_pattern,
                root=repo_root,
            )
            total = len(items)
            shown = items[: args.limit] if args.limit else items
            if getattr(args, "output_format", "text") == "json":
                # Was a bare JSON array, which the envelope schema does not
                # permit as `data` and which carried no size, provenance or
                # paging. `items` is the conventional collection name, so the
                # shared emit path pages it without a special case.
                _emit_tool_result(args, {
                    "status": "ok",
                    "summary": f"Found {total} dead code symbol(s)"
                               + (f", showing {len(shown)}" if len(shown) < total else "")
                               + ".",
                    "items": shown,
                    "total": total,
                    "truncated": len(shown) < total,
                }, repo_root=repo_root)
            else:
                from . import compact as _compact
                from . import repo_paths as _paths

                print(f"Dead code: {total} item(s); showing {len(shown)}")
                for item in _paths.relativise_result(shown, repo_root):
                    print(f"  {_compact.node_row(item)}")

        elif args.command == "build":
            pp = (
                "none"
                if getattr(args, "skip_postprocess", False)
                else ("minimal" if getattr(args, "skip_flows", False) else "full")
            )
            from .tools.build import build_or_update_graph

            previous_disable = logging.root.manager.disable
            if args.quiet:
                logging.disable(logging.INFO)
            try:
                result = build_or_update_graph(
                    full_rebuild=True,
                    repo_root=str(repo_root),
                    postprocess=pp,
                    **embedding_refresh_kwargs,
                )
            finally:
                logging.disable(previous_disable)
            parsed = result.get("files_parsed", 0)
            nodes = result.get("total_nodes", 0)
            edges = result.get("total_edges", 0)
            if not args.quiet:
                print(
                    f"Full build: {parsed} files, {nodes} nodes, {edges} edges "
                    f"(postprocess={pp})"
                )
                if result.get("errors"):
                    print(f"Errors: {len(result['errors'])}")

        elif args.command == "update":
            pp = (
                "none"
                if getattr(args, "skip_postprocess", False)
                else ("minimal" if getattr(args, "skip_flows", False) else "full")
            )
            from .tools.build import build_or_update_graph

            previous_disable = logging.root.manager.disable
            if args.quiet:
                logging.disable(logging.INFO)
            try:
                result = build_or_update_graph(
                    full_rebuild=False,
                    repo_root=str(repo_root),
                    base=args.base,
                    postprocess=pp,
                    **embedding_refresh_kwargs,
                )
            except RuntimeError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(1)
            finally:
                logging.disable(previous_disable)
            nodes = result.get("total_nodes", 0)
            edges = result.get("total_edges", 0)
            if not args.quiet:
                if result.get("build_type") == "full":
                    # No usable incremental base (fresh/legacy graph, or the
                    # last-synced commit was lost to a rewrite/shallow clone),
                    # so the update fell back to a full rebuild.
                    parsed = result.get("files_parsed", 0)
                    print(
                        f"Full rebuild (no usable incremental base): "
                        f"{parsed} files, {nodes} nodes, {edges} edges"
                        f" (postprocess={pp})"
                    )
                else:
                    updated = result.get("files_updated", 0)
                    print(
                        f"Incremental: {updated} files updated, "
                        f"{nodes} nodes, {edges} edges"
                        f" (postprocess={pp})"
                    )

            # --brief: append a one-line change-impact summary with the same
            # estimated context-savings approximation that detect-changes uses.
            # Same baseline (changed files vs analysis response), so the two
            # commands are directly comparable.
            if getattr(args, "brief", False) and not args.quiet:
                from .changes import analyze_changes
                from .context_savings import (
                    attach_context_savings,
                    estimate_file_tokens,
                    format_context_savings_panel,
                )
                from .incremental import (
                    get_changed_files,
                    get_staged_and_unstaged,
                )

                # Reuse the base the update actually resolved to (args.base is
                # None by default now, which get_changed_files cannot accept).
                brief_base = result.get("base_resolved") or "HEAD~1"
                changed = get_changed_files(repo_root, brief_base)
                if not changed:
                    changed = get_staged_and_unstaged(repo_root)
                if changed:
                    impact = analyze_changes(
                        store,
                        changed,
                        repo_root=str(repo_root),
                        base=brief_base,
                    )
                    original_tokens = estimate_file_tokens(repo_root, changed)
                    attach_context_savings(
                        impact,
                        original_tokens=original_tokens,
                    )
                    summary = impact.get("summary", "")
                    if summary:
                        print(summary)
                    verified = None
                    if getattr(args, "verify", False):
                        from .context_savings import verify_with_tiktoken
                        verified = verify_with_tiktoken(
                            repo_root, changed, impact,
                        )
                        if verified is None:
                            print(
                                "Note: --verify requires tiktoken. "
                                "Install with `pip install tiktoken`.",
                            )
                    panel = format_context_savings_panel(
                        impact.get("context_savings"),
                        original_tokens=original_tokens,
                        response=impact,
                        verified=verified,
                    )
                    if panel:
                        print(panel)

        elif args.command == "status":
            stats = store.get_stats()
            stored_branch = store.get_metadata("git_branch")
            stored_sha = store.get_metadata("git_head_sha")
            from .incremental import _git_branch_info, detect_vcs

            vcs = detect_vcs(repo_root)
            current_branch = None
            current_sha = None
            if vcs == "git":
                current_branch, current_sha = _git_branch_info(repo_root)
            stored_svn_branch = store.get_metadata("svn_branch")
            stored_rev = store.get_metadata("svn_revision")

            # --json is the deprecated alias for --format json.
            want_json = args.json_output or getattr(args, "output_format", "text") == "json"

            if want_json:
                from . import envelope as _env

                stale = bool(
                    stored_branch and current_branch and stored_branch != current_branch
                )
                env = _env.ok(
                    "status",
                    data={
                        "nodes": stats.total_nodes,
                        "edges": stats.total_edges,
                        "files": stats.files_count,
                        "languages": list(stats.languages),
                        "last_updated": stats.last_updated,
                        "vcs": vcs,
                        "built_on_branch": stored_branch,
                        "built_at_commit": stored_sha,
                        "current_branch": current_branch,
                        "current_sha": current_sha,
                        "svn_branch": stored_svn_branch,
                        "svn_revision": stored_rev,
                        # Explicit rather than left for the agent to infer by
                        # comparing branches itself.
                        "stale": stale,
                    },
                    provenance={
                        "graph_sha": stored_sha,
                        "built_at": stats.last_updated,
                    },
                )
                return _env.emit(env, "json", getattr(args, "max_tokens", None))
            elif not args.quiet:
                print(f"Nodes: {stats.total_nodes}")
                print(f"Edges: {stats.total_edges}")
                print(f"Files: {stats.files_count}")
                print(f"Languages: {', '.join(stats.languages)}")
                print(f"Last updated: {stats.last_updated or 'never'}")
                if stored_branch:
                    print(f"Built on branch: {stored_branch}")
                if stored_sha:
                    print(f"Built at commit: {stored_sha[:12]}")
                if stored_branch and current_branch and stored_branch != current_branch:
                    print(
                        f"WARNING: Graph was built on '{stored_branch}' "
                        f"but you are now on '{current_branch}'. "
                        f"Run 'carto build' to rebuild."
                    )
                if vcs == "svn":
                    if stored_svn_branch:
                        print(f"SVN branch: {stored_svn_branch}")
                    if stored_rev:
                        print(f"SVN revision at build: {stored_rev}")

        elif args.command == "forget":
            stored_files = store.get_all_files()
            targets = _match_files_to_forget(stored_files, args.paths, repo_root)
            if not targets:
                print("No parsed files matched the given path(s).")
                print(f"The graph currently tracks {len(stored_files)} file(s).")
            else:
                header = (
                    "[dry-run] Would forget these files:"
                    if args.dry_run
                    else "Forgetting these files:"
                )
                print(header)
                for file_path in targets:
                    try:
                        display = os.path.relpath(file_path, str(repo_root))
                    except ValueError:
                        display = file_path
                    print(f"  {display}")
                if args.dry_run:
                    print(
                        f"\n[dry-run] {len(targets)} file(s) would be removed "
                        "from the graph. No changes made."
                    )
                else:
                    from .forget import forget_files

                    summary = forget_files(store, repo_root, targets)
                    reparsed = summary.get("reparsed", [])
                    if reparsed:
                        print(
                            f"  re-resolved {len(reparsed)} referring file(s) "
                            "so no edges dangle"
                        )
                    remaining = len(stored_files) - len(targets)
                    print(
                        f"\nForgot {len(targets)} file(s); "
                        f"{remaining} file(s) remain in the graph."
                    )

        elif args.command == "watch":
            from .postprocessing import run_post_processing

            try:
                callback = (
                    partial(run_post_processing, **embedding_refresh_kwargs)
                    if embedding_refresh_kwargs
                    else run_post_processing
                )
                watch(repo_root, store, on_files_updated=callback)
            except RuntimeError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                sys.exit(1)

        elif args.command == "visualize":
            from .incremental import get_data_dir

            # Prefer an explicit --data-dir so read-only resolution still
            # writes exports next to the graph without registry side-effects.
            if getattr(args, "data_dir", None):
                data_dir = Path(args.data_dir).expanduser().resolve()
                data_dir.mkdir(parents=True, exist_ok=True)
            else:
                data_dir = get_data_dir(repo_root)
            fmt = getattr(args, "format", "html") or "html"

            if fmt == "json":
                from .exports import export_json

                out = data_dir / "graph.json"
                export_json(store, out)
                print(f"JSON exported: {out}")
            elif fmt == "graphml":
                from .exports import export_graphml

                out = data_dir / "graph.graphml"
                export_graphml(store, out)
                print(f"GraphML exported: {out}")
            elif fmt == "cypher":
                from .exports import export_neo4j_cypher

                out = data_dir / "graph.cypher"
                export_neo4j_cypher(store, out)
                print(f"Neo4j Cypher exported: {out}")
            elif fmt == "obsidian":
                from .exports import export_obsidian_vault

                out = data_dir / "obsidian"
                export_obsidian_vault(store, out)
                print(f"Obsidian vault exported: {out}")
            elif fmt == "svg":
                from .exports import export_svg

                out = data_dir / "graph.svg"
                export_svg(store, out)
                print(f"SVG exported: {out}")
            else:
                from .visualization import generate_html

                html_path = data_dir / "graph.html"
                vis_mode = getattr(args, "mode", "auto") or "auto"
                generate_html(store, html_path, mode=vis_mode)
                print(f"Visualization ({vis_mode}): {html_path}")
                if getattr(args, "serve", False):
                    import functools
                    import http.server

                    serve_dir = html_path.parent
                    port = 8765
                    http_handler = functools.partial(
                        http.server.SimpleHTTPRequestHandler,
                        directory=str(serve_dir),
                    )
                    print(f"Serving at http://localhost:{port}/graph.html")
                    print("Press Ctrl+C to stop.")
                    with http.server.HTTPServer(("localhost", port), http_handler) as httpd:
                        try:
                            httpd.serve_forever()
                        except KeyboardInterrupt:
                            print("\nServer stopped.")
                else:
                    print("Open in browser to explore.")

        elif args.command == "wiki":
            from .incremental import get_data_dir
            from .wiki import generate_wiki

            if getattr(args, "data_dir", None):
                data_dir = Path(args.data_dir).expanduser().resolve()
                data_dir.mkdir(parents=True, exist_ok=True)
            else:
                data_dir = get_data_dir(repo_root)
            wiki_dir = data_dir / "wiki"
            result = generate_wiki(store, wiki_dir, force=args.force)
            total = result["pages_generated"] + result["pages_updated"] + result["pages_unchanged"]
            print(
                f"Wiki: {result['pages_generated']} new, "
                f"{result['pages_updated']} updated, "
                f"{result['pages_unchanged']} unchanged "
                f"({total} total pages)"
            )
            print(f"Output: {wiki_dir}")

        elif args.command == "detect-changes":
            from .changes import analyze_changes
            from .context_savings import (
                attach_context_savings,
                estimate_file_tokens,
            )
            from .incremental import get_changed_files, get_staged_and_unstaged

            base = args.base
            changed = get_changed_files(repo_root, base)
            if not changed:
                changed = get_staged_and_unstaged(repo_root)

            original_tokens = 0
            if not changed:
                # An empty change set is a correct answer, not a failure, so it
                # is a success envelope with an empty payload rather than the
                # bare line of prose it used to print.
                result = {"summary": "No changes detected.", "risk_score": 0.0}
            else:
                result = analyze_changes(
                    store,
                    changed,
                    repo_root=str(repo_root),
                    base=base,
                    include_churn=getattr(args, "churn", False),
                )
                original_tokens = estimate_file_tokens(repo_root, changed)
                attach_context_savings(
                    result,
                    original_tokens=original_tokens,
                )

            brief = getattr(args, "brief", False)
            if brief and args.output_format != "json":
                from .context_savings import (
                    format_context_savings_panel,
                    verify_with_tiktoken,
                )
                print(result.get("summary", "No summary available."))
                verified = None
                if changed and getattr(args, "verify", False):
                    verified = verify_with_tiktoken(repo_root, changed, result)
                    if verified is None:
                        print(
                            "Note: --verify requires tiktoken. "
                            "Install with `pip install tiktoken`.",
                        )
                panel = format_context_savings_panel(
                    result.get("context_savings"),
                    original_tokens=original_tokens,
                    response=result,
                    verified=verified,
                )
                if panel:
                    print(panel)
            else:
                if brief:
                    # In json, brief cannot mean "render a panel", so it means
                    # what the panel actually says: the verdict without the
                    # per-node detail behind it.
                    result = {
                        k: v for k, v in result.items()
                        if k in ("summary", "risk_score", "context_savings")
                    }
                _emit_tool_result(args, result, repo_root=repo_root)

    finally:
        store.close()
