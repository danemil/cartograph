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
COMMANDS = ("mem add", "mem search", "mem show", "mem status", "mem summarise", "mem sync")


def add_parser(sub: Any) -> argparse.ArgumentParser:
    """Build `carto mem` and its subcommands.

    Every flag hangs off a *sub*command, none off `mem` itself. argparse would
    require a parent-level flag before the subcommand — `carto mem --repo . search`
    — which is an ordering no agent infers from a catalogue entry.
    """
    mem_cmd = sub.add_parser("mem", help="Recorded observations: add, search, inspect")
    mem_sub = mem_cmd.add_subparsers(
        dest="mem_command", metavar="{add,search,show,status,summarise,sync}"
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
        help="Host that produced it, e.g. copilot-cli, copilot-chat",
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

    show_cmd = mem_sub.add_parser(
        "show", help="Whole observations by id, after a search has found them"
    )
    show_cmd.add_argument(
        "--id", dest="ids", action="append", required=True,
        help="Observation id from a search result (repeatable)",
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
            "agent, so no Copilot quota is spent"
        ),
    )
    summarise_cmd.add_argument(
        "--fallback-reason", dest="fallback_reason", default=None, metavar="TEXT",
        help=(
            "With --no-host-agent: why no model could write this summary, kept in the "
            "store so `mem status` can say it"
        ),
    )

    summarise_cmd.add_argument(
        "--brief-only", dest="brief_only", action="store_true",
        help=(
            "Return the brief a host agent would be sent, and write nothing — for a "
            "caller that will get the summary from a model itself"
        ),
    )
    summarise_cmd.add_argument(
        "--answer-file", dest="answer_file", default=None, metavar="PATH",
        help=(
            "Store the summary a caller obtained for --session, read from this file, "
            "as a host-agent summary (needs --summarised-by)"
        ),
    )
    summarise_cmd.add_argument(
        "--summarised-by", dest="summarised_by", default=None, metavar="LABEL",
        help="With --answer-file: who wrote it, as vscode-lm:<model-id>",
    )
    summarise_cmd.add_argument(
        "--hand-off", dest="hand_off", choices=list(_summarise.HAND_OFF_MODES), default=None,
        help=(
            "With --pending: list the sessions awaiting a summary and write nothing, "
            "when no host agent CLI is on PATH (no-cli), or always"
        ),
    )
    _add_log_dirs(summarise_cmd)

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
            "Afterwards, summarise sessions whose last message is "
            f"{_sync.SETTLE_SECONDS // 60} minutes old; `waiting` lists the ones "
            "not yet settled"
        ),
    )
    _add_log_dirs(sync_cmd)
    sync_cmd.add_argument(
        "--no-host-agent", dest="no_host_agent", action="store_true",
        help="With --summarise: write structural summaries without calling a host agent",
    )
    sync_cmd.add_argument(
        "--fallback-reason", dest="fallback_reason", default=None, metavar="TEXT",
        help=(
            "With --no-host-agent: why no model could write these summaries, kept in the "
            "store so `mem status` can say it"
        ),
    )
    sync_cmd.add_argument(
        "--hand-off", dest="hand_off", choices=list(_summarise.HAND_OFF_MODES), default=None,
        help=(
            "With --summarise: list the sessions awaiting a summary instead of writing "
            "structural ones, for a caller that can summarise them — when no host "
            "agent CLI is on PATH (no-cli), or always"
        ),
    )

    for command in (add_cmd, search_cmd, show_cmd, status_cmd, summarise_cmd, sync_cmd):
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


def _add_log_dirs(command: argparse.ArgumentParser) -> None:
    """Where the Chat logs are, for sync and for the replies in a brief.

    Both commands read the same logs, and a brief built without the directory
    the extension named would silently lose every reply a remote window has.
    """
    command.add_argument(
        "--vscode-user-dir", dest="vscode_user_dirs", action="append", default=None,
        help="A VS Code User directory to read Chat logs from (repeatable; auto-detected)",
    )
    command.add_argument(
        "--vscode-workspace-dir", dest="vscode_workspace_dirs", action="append", default=None,
        help=(
            "A VS Code workspaceStorage/<id> directory whose chat logs belong to this "
            "repository (repeatable; for callers that know it, like the extension)"
        ),
    )


def _log_dirs(args: argparse.Namespace) -> "tuple[Optional[list[Path]], list[Path]]":
    users = getattr(args, "vscode_user_dirs", None)
    return (
        [Path(d).expanduser() for d in users] if users else None,
        [Path(d).expanduser() for d in getattr(args, "vscode_workspace_dirs", None) or []],
    )


def _usage_exit(command: str, message: str, fmt: str) -> None:
    """A well-formed parse whose flags contradict each other: exit 1, like argparse's."""
    from .. import envelope as _env

    raise SystemExit(_env.emit(_env.error(command, _env.Exit.USAGE, message), fmt))


def _summarise_usage(args: argparse.Namespace) -> Optional[str]:
    """What is wrong with this `mem summarise` flag set, or None.

    Checked before anything is read or written: each of these combinations
    would otherwise do one of the two things asked and quietly drop the other.
    """
    from . import summarise as _summarise

    if args.brief_only and (args.no_host_agent or args.answer_file or args.pending):
        return "--brief-only writes nothing; it cannot be combined with " \
            "--no-host-agent, --answer-file or --pending"
    if bool(args.answer_file) != bool(args.summarised_by):
        return "--answer-file and --summarised-by go together"
    if args.answer_file and (args.pending or args.no_host_agent):
        return "--answer-file stores one session's answer; not with --pending or --no-host-agent"
    if args.hand_off and (not args.pending or args.no_host_agent):
        return "--hand-off needs --pending, and not --no-host-agent"
    if args.fallback_reason is not None and not args.no_host_agent:
        return "--fallback-reason explains a structural summary; it needs --no-host-agent"
    if args.summarised_by and not _summarise.ANSWER_LABEL.match(args.summarised_by):
        return f"--summarised-by must be vscode-lm:<model-id>, not {args.summarised_by!r}"
    return None


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

        if args.hand_off and (args.no_host_agent or not args.summarise):
            _usage_exit(
                command, "--hand-off needs --summarise, and not --no-host-agent", fmt
            )
        if args.fallback_reason is not None and not (args.summarise and args.no_host_agent):
            _usage_exit(
                command, "--fallback-reason needs --summarise and --no-host-agent", fmt
            )
        user_dirs, workspace_dirs = _log_dirs(args)
        result = _sync.sync(
            repo_root,
            user_dirs=user_dirs,
            workspace_dirs=workspace_dirs,
            project=args.project,
            summarise_sessions=args.summarise,
            use_host=not args.no_host_agent,
            hand_off=args.hand_off,
            fallback_reason=args.fallback_reason,
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

    if args.mem_command == "summarise":
        problem = _summarise_usage(args)
        if problem:
            _usage_exit(command, problem, fmt)

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

        if args.pending and _summarise.hands_off(
            use_host=not args.no_host_agent, hand_off=args.hand_off
        ):
            waiting = _summarise.awaiting(
                repo_root, exclude=[args.exclude_session] if args.exclude_session else []
            )
            result = {
                "summary": f"{len(waiting)} session(s) left for a caller to summarise; "
                "nothing written.",
                "sessions": [],
                "awaiting_summary": waiting,
            }
        elif args.pending:
            ran = _summarise.summarise_pending(
                repo_root, exclude=args.exclude_session, project=args.project,
                use_host=not args.no_host_agent, fallback_reason=args.fallback_reason,
            )
            result = {
                "summary": f"Summarised {sum(1 for r in ran if r.get('observation'))} "
                f"of {len(ran)} pending session(s).",
                "sessions": ran,
            }
        else:
            answer = None
            if args.answer_file:
                try:
                    answer = Path(args.answer_file).expanduser().read_text(encoding="utf-8")
                except OSError as exc:
                    _usage_exit(command, f"--answer-file could not be read: {exc}", fmt)
            user_dirs, workspace_dirs = _log_dirs(args)
            result = _summarise.summarise(
                repo_root, session=args.session, project=args.project,
                use_host=not args.no_host_agent,
                user_dirs=user_dirs, workspace_dirs=workspace_dirs,
                brief_only=args.brief_only,
                answer=answer, summarised_by=args.summarised_by,
                fallback_reason=args.fallback_reason,
            )
        with _store.MemoryStore(path) as memory:
            provenance = memory.provenance()
        _emit_tool_result(
            args, result, command=command, provenance=provenance, repo_root=repo_root
        )
        return

    with _store.MemoryStore(path) as memory:
        provenance = memory.provenance()
        if args.mem_command == "show":
            items = memory.get_by_ids(args.ids)
            result = {
                "summary": f"{len(items)} of {len(set(args.ids))} observation(s) found",
                "items": items,
                "missing": [i for i in dict.fromkeys(args.ids)
                            if i not in {item["id"] for item in items}],
            }
            _count_served(memory, result)
            _emit_tool_result(
                args, result, command=command, provenance=provenance, repo_root=repo_root
            )
            return

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
        semantic_reason = None
        if mode == "fts":
            # Everything was in place and the query itself failed — the model
            # raised, or the index did not answer — is the one case status
            # cannot name, because it holds only for this query.
            semantic_reason = memory.semantic_status()[1] or (
                "the embedding model or the vector index failed on this query"
            )
        _count_served(memory, result)

    if mode == "fts":
        # Keyword with no reason is a dead end: the agent cannot tell a store
        # that has no model from one that has not been embedded yet.
        result["semantic_unavailable"] = semantic_reason
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


#: What recall has handed to agents: how many responses, and their size at the
#: same chars/4 the envelope reports. Counted where the response is built, so
#: the figure is what was served, not what could have been.
_SERVED_COUNT = "cost:served_count"
_SERVED_TOKENS = "cost:served_tokens"


def _count_served(memory: _store.MemoryStore, result: dict[str, Any]) -> None:
    import json

    memory.add_to_counter(_SERVED_COUNT, 1)
    memory.add_to_counter(_SERVED_TOKENS, len(json.dumps(result, default=str)) // 4)


def _cost_lines(memory: _store.MemoryStore) -> dict[str, str]:
    """What memory cost and what it replaced — only what can be counted.

    Unlike claude-mem's Stats line, nothing here compares a model's own spend
    with an estimate of what it wrote, and nothing is counted twice. Token
    figures are chars/4 and say so; Copilot calls are counted exactly.
    """
    from . import summarise as _summarise

    calls = int(memory.get_meta(_summarise.COST_HOST_CALLS) or 0)
    served = int(memory.get_meta(_SERVED_COUNT) or 0)
    served_tokens = int(memory.get_meta(_SERVED_TOKENS) or 0)
    raw = memory.meta_with_prefix(_summarise.COST_RAW_CHARS)
    lines: dict[str, str] = {}
    if calls or served:
        lines["memory_cost"] = (
            f"{calls} summary call(s) to Copilot · {served} recall(s) served "
            f"≈ {served_tokens:,} tokens (chars/4)"
        )
    if raw:
        sessions = [key[len(_summarise.COST_RAW_CHARS):] for key in raw]
        marks = ",".join("?" * len(sessions))
        summary_chars = memory._conn.execute(  # noqa: SLF001 — one aggregate, here only
            f"SELECT coalesce(sum(length(title) + length(body)), 0) FROM observations "
            f"WHERE doc_type = 'sessions' AND session IN ({marks})",
            sessions,
        ).fetchone()[0]
        raw_tokens = sum(int(v) for v in raw.values()) // 4
        lines["vs_raw_logs"] = (
            f"{len(raw)} summarised session(s): summaries ≈ {summary_chars // 4:,} tokens "
            f"vs their prompts + replies ≈ {raw_tokens:,} tokens (chars/4)"
        )
    return lines


def _latest_summary_by(memory: _store.MemoryStore) -> Optional[str]:
    """Who wrote the newest session summary: a host's label, or ``structural``.

    What the status bar shows, read from the row itself rather than from
    whichever process last ran, so a summary written by the CLI in a terminal
    and one written through VS Code are reported alike. A structural one
    carries its recorded reason, ``structural (<reason>)``: a bare "structural"
    left the one person who hit it with no way to learn why.
    """
    from . import summarise as _summarise

    row = memory._conn.execute(  # noqa: SLF001 — one lookup, here only
        "SELECT platform_source, summary_source, session FROM observations "
        "WHERE doc_type = 'sessions' ORDER BY created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    if row[1] != "host-agent":
        reason = _summarise.fallback_reason_for(memory, row[2]) if row[2] else None
        return f"structural ({reason})" if reason else "structural"
    return row[0] or "host-agent"


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
        "embedded_observations": memory.vector_count(),
    }
    if reason:
        result["semantic_search_unavailable"] = reason
    # How prompts reached the store, per host, from the last `mem sync` that saw
    # any: "hooks" when every logged prompt was already recorded, "logs" when
    # some were not — hooks blocked or not firing, and the logs carried it.
    from . import sync as _sync

    result.update(_cost_lines(memory))
    latest = _latest_summary_by(memory)
    if latest:
        result["latest_summary_by"] = latest
    for host, stats in _sync.last_status(memory).items():
        result[f"capture_{host.replace('-', '_')}"] = (
            f"{stats.get('capture', 'unknown')} "
            f"({stats.get('imported', 0)} imported, "
            f"{stats.get('already_recorded', 0)} already recorded, at {stats.get('at', '?')})"
        )
    return result
