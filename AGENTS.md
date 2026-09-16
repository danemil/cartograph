# Cartograph

This repo uses `carto` — a knowledge-graph view of the codebase, as a plain
CLI. There is no MCP server, by design.

**If your tool reads Agent Skills** (`.claude/skills/`, `.github/skills/`,
`.agents/skills/`), use those instead of this file. They are more specific and
they are verified against the CLI on every change; this section is only the
fallback for a host that discovers neither.

No graph yet? `carto build`. Otherwise pick by task:

| Task | Skill | Or, directly |
|---|---|---|
| Reviewing a diff, PR, or branch | `review-changes` | `carto review-summary --format json` |
| Understanding unfamiliar code | `explore-codebase` | `carto architecture --format json` |
| Chasing a bug | `debug-issue` | `carto query callers_of <symbol> --format json` |
| Planning a rename or cleanup | `refactor-safely` | `carto refactor rename --old-name <old> --new-name <new> --format json` |
| Building or refreshing the graph | `build-graph` | `carto status --format json` |

Always pass `--format json`. Every command returns one envelope on stdout.

- Exit `0` success — an empty result is success, not an error.
- Exit `1` the call was malformed.
- Exit `2` a precondition is unmet. `error.remediation` names the command that
  fixes it: run that, then retry, rather than falling back to reading files.
- Exit `3` internal error.

`--max-tokens N` bounds any response. Truncation is semantic, never a byte cut,
so the result is always valid JSON; check `truncated_reason` and
`size.over_budget`.

Full command reference: `carto capabilities --format json`, or
`carto capabilities --command <name>` for one command's flags. Do not rely on a
command list written down anywhere else — that catalogue is generated from the
CLI itself, so it cannot drift.
