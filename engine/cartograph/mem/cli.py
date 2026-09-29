"""`carto mem` — the parser and the dispatch.

Everything after the argument parsing is borrowed. Paging, cursors, the token
budget, path relativisation and the envelope itself all belong to
``cartograph.cli._emit_tool_result``, which the graph commands already use, so
`mem search` pages exactly the way `search` does and cannot drift from it.
That is deliberate: the previous two defects in this project were both a second
copy of something that already existed.

The imports from ``..cli`` are inside functions because ``cli`` builds this
parser while it is still executing its own module body.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Optional

from . import store as _store

#: Named here so the dispatch, the catalogue and the pageable-collection table
#: all spell the operation the same way. It is also the `tool` field an agent
#: sees, which the envelope schema permits to carry a space.
COMMANDS = ("mem add", "mem search", "mem status", "mem summarise", "mem sync")


def add_parser(sub: Any) -> argparse.ArgumentParser:
    """Build `carto mem` and its subcommands.

    Every flag hangs off a *sub*command, none off `mem` itself. argparse would
    require a parent-level flag before the subcommand — `carto mem --repo . search`
    — which is an ordering no agent infers from a catalogue entry.
    """
    mem_cmd = sub.add_parser("mem", help="Recorded observations: add, search, inspect")
    mem_sub = mem_cmd.add_subparsers(
        dest="mem_command", metavar="{add,search,status,summarise,sync}"
    )
    # Without this, `carto mem` alone parses cleanly and dispatches to nothing.
    # Required makes argparse route it through _ContractParser.error, which is
    # the usage envelope an agent can read.
    mem_sub.required = True

    add_cmd = mem_sub.add_parser("add", help="Record one observation")
    add_cmd.add_argument("--title", required=True, help="One-line summary of the observation")
    add_cmd.add_argument("--body", default="", help="The observation itself")
    add_cmd.add_argument(
        "--kind", default="note", help="Observation type, e.g. decision, bug, note"
    )
    add_cmd.add_argument("--session", default=None, help="Session this observation belongs to")
    add_cmd.add_argument(
        "--doc-type", dest="doc_type", choices=list(_store.DOC_TYPES),
        default="observations", help="Record type",
    )
    add_cmd.add_argument(
        "--platform-source", dest="platform_source", default=None,
        help="Host that produced it, e.g. claude-code, copilot-cli",
    )
    add_cmd.add_argument(
        "--summary-source", dest="summary_source", choices=list(_store.SUMMARY_SOURCES),
        default="verbatim",
        help="Where the text came from; 'verbatim' means the caller wrote it, unsummarised",
    )
    add_cmd.add_argument(
        "--file", action="append", dest="files", default=None, metavar="PATH",
        help="A file the observation is about (repeatable; stored repo-relative)",
    )

    search_cmd = mem_sub.add_parser("search", help="Find recorded observations")
    search_cmd.add_argument("--query", required=True, help="What to look for")
    search_cmd.add_argument(
        "--obs-type", dest="obs_types", default=None,
        help="Comma-separated observation kinds to keep",
    )
    search_cmd.add_argument(
        "--doc-type", dest="doc_type", choices=list(_store.DOC_TYPES), default=None,
        help="Restrict to one record type",
    )
    search_cmd.add_argument(
        "--platform-source", dest="platform_source", default=None,
        help="Restrict to observations recorded by one host",
    )
    search_cmd.add_argument("--session", default=None, help="Restrict to one session")
    search_cmd.add_argument(
        "--date-start", dest="date_start", default=None, metavar="DATE",
        help="Earliest creation date, YYYY-MM-DD or a full timestamp",
    )
    search_cmd.add_argument(
        "--date-end", dest="date_end", default=None, metavar="DATE",
        help="Latest creation date, inclusive of the day named",
    )
    search_cmd.add_argument(
        "--order-by", dest="order_by", choices=list(_store.ORDER_BY), default="relevance",
        help="Result order",
    )
    search_cmd.add_argument(
        "--limit", type=_positive_int, default=25,
        help="Maximum observations returned (the pageable collection)",
    )
    search_cmd.add_argument(
        "--cursor", default=None,
        help="Continue from a previous page (opaque; pass page.next_cursor verbatim)",
    )

    status_cmd = mem_sub.add_parser(
        "status", help="Whether a memory store exists, and what is in it"
    )

    from . import summarise as _summarise
    from . import sync as _sync

    summarise_cmd = mem_sub.add_parser(
        "summarise",
        help="Synthesise one session's captured prompts into a single observation",
    )
    summarise_cmd.add_argument(
        "--session", default=None,
        help="Session to summarise (defaults to the one that recorded the latest prompt)",
    )
    summarise_cmd.add_argument(
        "--pending", action="store_true",
        help=(
            "Summarise every recent session that has none yet, instead of one "
            f"(at most {_summarise.MAX_PENDING} per run)"
        ),
    )
    summarise_cmd.add_argument(
        "--exclude-session", default=None, dest="exclude_session",
        help="With --pending: a session to leave alone, normally the one just starting",
    )
    # store_true rather than a store_false spelled `use_host`: the catalogue
    # reports an action's default verbatim, and "--no-host-agent, default: true"
    # reads to an agent as though the flag were already on.
    summarise_cmd.add_argument(
        "--no-host-agent", dest="no_host_agent", action="store_true",
        help=(
            "Write the deterministic structural summary without calling a host "
            "agent, so no Copilot or Claude quota is spent"
        ),
    )

    sync_cmd = mem_sub.add_parser(
        "sync",
        help=(
            "Import prompts from Copilot's own conversation logs that no hook "
            "recorded, and report whether hooks are firing"
        ),
    )
    sync_cmd.add_argument(
        "--summarise", action="store_true",
        help=(
            "Afterwards, summarise sessions whose logs have been quiet for "
            f"{_sync.SETTLE_SECONDS // 60} minutes"
        ),
    )
    sync_cmd.add_argument(
        "--vscode-user-dir", dest="vscode_user_dirs", action="append", default=None,
        help="A VS Code User directory to read Chat logs from (repeatable; auto-detected)",
    )
    sync_cmd.add_argument(
        "--no-host-agent", dest="no_host_agent", action="store_true",
        help="With --summarise: write structural summaries without calling a host agent",
    )

    for command in (add_cmd, search_cmd, status_cmd, summarise_cmd, sync_cmd):
        command.add_argument(
            "--project", default=None,
            help="Project name (defaults to the repository directory)",
        )
        command.add_argument("--repo", default=None, help="Repository root (auto-detected)")
        command.add_argument(
            "--format", choices=["json", "text"], default="json", dest="output_format",
            help="Output format (these commands default to json — they are agent-facing)",
        )
        command.add_argument(
            "--max-tokens", type=int, default=None, dest="max_tokens",
            help="Token budget for the response",
        )
    return mem_cmd


def _positive_int(value: str) -> int:
    """The CLI's own limit type, so `--limit 0` is one usage error everywhere."""
    from ..cli import _positive_int as shared

    return shared(value)


def run(args: argparse.Namespace, repo_root: Path) -> None:
    """Dispatch one `carto mem` subcommand. Always exits through the envelope."""
    from ..cli import _emit_tool_result, _open_page, _precondition_exit

    command = f"mem {args.mem_command}"
    fmt = getattr(args, "output_format", None) or "json"
    project = args.project or repo_root.name

    if args.mem_command == "add":
        # Stored relative, not just emitted relative. An observation outlives
        # the checkout it was recorded in — a colleague's clone, a CI worker —
        # and an absolute path would be the one field that does not survive
        # the move. Resolved first because `--file` is relative to the caller's
        # working directory, which need not be the repository root.
        from .. import repo_paths as _paths

        files = [
            _paths.relativise(str(Path(name).expanduser().resolve()), repo_root)
            for name in (args.files or [])
        ]
        path = _store.db_path(repo_root, create=True)
        with _store.MemoryStore(path) as memory:
            observation = memory.add(
                project=project,
                title=args.title,
                body=args.body,
                kind=args.kind,
                session=args.session,
                doc_type=args.doc_type,
                platform_source=args.platform_source,
                summary_source=args.summary_source,
                file_paths=files,
            )
            provenance = memory.provenance()
        result = {
            "summary": f"Recorded observation {observation['id']}",
            "observation": observation,
        }
        _emit_tool_result(
            args, result, command=command, provenance=provenance, repo_root=repo_root
        )
        return

    if args.mem_command == "sync":
        # Before the store check: sync is what creates the store on a machine
        # where hooks never ran, so "no store yet" is its normal starting point.
        from . import sync as _sync

        result = _sync.sync(
            repo_root,
            user_dirs=[Path(d).expanduser() for d in args.vscode_user_dirs]
            if args.vscode_user_dirs else None,
            project=args.project,
            summarise_sessions=args.summarise,
            use_host=not args.no_host_agent,
        )
        synced = _store.db_path(repo_root, create=False)
        provenance = None
        if synced.exists():
            with _store.MemoryStore(synced) as memory:
                provenance = memory.provenance()
        _emit_tool_result(
            args, result, command=command, provenance=provenance, repo_root=repo_root
        )
        return

    path = _store.db_path(repo_root, create=False)
    if not path.exists():
        from .. import repo_paths as _paths

        # Exit 2 with the command that fixes it, like every other precondition:
        # an agent that has never recorded anything should be able to recover
        # without asking a human what to run.
        _precondition_exit(
            command,
            f"No memory store at {_paths.relativise(str(path), repo_root)}.",
            "carto mem add --title <title> --body <text>",
            fmt=fmt,
        )

    if args.mem_command == "summarise":
        # Its own branch because it both reads and writes, and because the host
        # call inside it must not happen with the store open — see
        # `summarise.summarise`. Provenance is taken afterwards, so the response
        # describes the store including the row just written.
        from . import summarise as _summarise

        if args.pending:
            ran = _summarise.summarise_pending(
                repo_root, exclude=args.exclude_session, project=args.project,
                use_host=not args.no_host_agent,
            )
            result = {
                "summary": f"Summarised {sum(1 for r in ran if r.get('observation'))} "
                f"of {len(ran)} pending session(s).",
                "sessions": ran,
            }
        else:
            result = _summarise.summarise(
                repo_root, session=args.session, project=args.project,
                use_host=not args.no_host_agent,
            )
        with _store.MemoryStore(path) as memory:
            provenance = memory.provenance()
        _emit_tool_result(
            args, result, command=command, provenance=provenance, repo_root=repo_root
        )
        return

    with _store.MemoryStore(path) as memory:
        provenance = memory.provenance()
        if args.mem_command == "status":
            _emit_tool_result(
                args, _status_result(memory, repo_root, path),
                command=command, provenance=provenance, repo_root=repo_root,
            )
            return

        offset, page_limit, query, snapshot = _open_page(
            args, repo_root, provenance, command=command
        )
        items, mode, relaxed = memory.search(
            query=args.query,
            project=args.project,
            session=args.session,
            doc_type=args.doc_type,
            kinds=_split_types(args.obs_types),
            platform_source=args.platform_source,
            date_start=args.date_start,
            date_end=args.date_end,
            order_by=args.order_by,
            limit=args.limit,
        )

    result = {
        "summary": f"Found {len(items)} observation(s) matching '{args.query}'",
        "items": items,
    }
    if relaxed:
        # Said plainly, because it changes how far the rows should be trusted:
        # these share SOME of the query's words, not all of them.
        result["match"] = "relaxed"
        result["match_note"] = (
            "No observation carried every word of the query, so it was retried "
            "requiring any of them. Rows sharing the most words rank first."
        )
    _emit_tool_result(
        args, result, command=command,
        offset=offset, page_limit=page_limit,
        query=query, snapshot=snapshot, provenance=provenance,
        repo_root=repo_root, search_mode=mode,
    )


def _split_types(value: Optional[str]) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _status_result(
    memory: _store.MemoryStore, repo_root: Path, path: Path
) -> dict[str, Any]:
    """What `carto status` answers for the graph, answered for the memory.

    The semantic reason is here rather than left to be inferred: `search_mode`
    on a search result can only say embeddings did not participate, and an
    agent cannot act on that without knowing which of the three preconditions
    is the missing one.
    """
    from .. import repo_paths as _paths

    stats = memory.stats()
    semantic, reason = memory.semantic_status()
    result: dict[str, Any] = {
        "summary": f"{stats['observations']} observation(s) in {repo_root.name}",
        "store": _paths.relativise(str(path), repo_root),
        "schema_version": _store.LATEST_VERSION,
        "semantic_search": semantic,
        **stats,
    }
    if reason:
        result["semantic_search_unavailable"] = reason
    # How prompts reached the store, per host, from the last `mem sync` that saw
    # any: "hooks" when every logged prompt was already recorded, "logs" when
    # some were not — hooks blocked or not firing, and the logs carried it.
    from . import sync as _sync

    for host, stats in _sync.last_status(memory).items():
        result[f"capture_{host.replace('-', '_')}"] = (
            f"{stats.get('capture', 'unknown')} "
            f"({stats.get('imported', 0)} imported, "
            f"{stats.get('already_recorded', 0)} already recorded, at {stats.get('at', '?')})"
        )
    return result
