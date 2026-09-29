#!/bin/bash
# The acceptance assertions. Runs inside the container, which is started with
# `--network none`.
#
# Two rules it holds itself to:
#
# 1. **Assert on the envelope, never on log text.** Every check below reads
#    `ok`, `error.code` or a field of `data` through jq. Prose is for the
#    person reading the output; a harness that greps it would pass on a
#    reworded message and fail on a rephrased one.
# 2. **Do not stop at the first failure.** Every assertion runs, and the ones
#    that failed are named again at the end. A run that dies on assertion 3
#    hides whatever assertion 19 would have told us.
#
# Exits non-zero if anything failed.

set -u

REPO=$HOME/work/app
CARTO_HOME=$HOME/.cartograph
LOG=$HOME/stderr.log
: >"$LOG"

failures=0
failed_names=()

pass() { printf '  ok    %-46s %s\n' "$1" "${2-}"; }
fail() {
    printf '  FAIL  %-46s %s\n' "$1" "${2-}"
    failed_names+=("$1: ${2-}")
    failures=$((failures + 1))
}

section() { printf '\n== %s ==\n' "$1"; }

# Captures stdout — which in json mode carries nothing but the envelope — and
# the exit code, keeping stderr in a log for the failure report.
ENV_JSON=""
RC=0
run_env() {
    ENV_JSON=$("$@" 2>>"$LOG")
    RC=$?
}

# jq -e so a null or false result is a non-zero exit rather than the string
# "null" being compared against something.
jqok() { printf '%s' "$ENV_JSON" | jq -e "$1" >/dev/null 2>&1; }
jqv() { printf '%s' "$ENV_JSON" | jq -r "$1" 2>/dev/null; }

# One helper for the shape every successful envelope shares, so the individual
# assertions below say only what is specific to them.
assert_ok_envelope() {
    local name=$1
    if [ "$RC" -ne 0 ]; then
        fail "$name" "exit $RC, expected 0"
    elif ! jqok '.ok == true'; then
        fail "$name" "ok is not true: $(jqv '.error.code // "unparseable"')"
    elif ! jqok '.schema == 1'; then
        fail "$name" "schema is $(jqv '.schema'), expected 1"
    elif ! jqok '.data != null'; then
        fail "$name" "data is null"
    else
        return 0
    fi
    return 1
}

printf 'Cartograph acceptance test\n'
printf 'container: %s %s\n' "$(uname -s)" "$(uname -m)"

# --------------------------------------------------------------------------
section "the network really is absent"

# --network none gives the container no attached interface. Asserted from
# /proc rather than with ip/ifconfig, neither of which is installed.
#
# Not "only lo exists": the kernel in Docker Desktop's VM has the gre/sit/tunl
# modules loaded, so their always-down stubs appear in every namespace and
# carry no traffic. What must be absent is an interface Docker would have
# attached — eth0 on any network other than none.
ifaces=$(awk 'NR>2 {sub(/:.*/,"",$1); print $1}' /proc/net/dev | tr -d ' ' | sort | tr '\n' ' ')
attached=$(printf '%s' "$ifaces" | tr ' ' '\n' | grep -E '^(eth|en|wl|docker)' | tr '\n' ' ')
if [ -z "$attached" ]; then
    pass "no attached network interface" "present: $ifaces"
else
    fail "no attached network interface" "found: $attached"
fi

if [ "$(awk 'NR>1' /proc/net/route | wc -l)" -eq 0 ]; then
    pass "no routes at all" "/proc/net/route is empty"
else
    fail "no routes at all" "routing table is not empty"
fi

# An active probe, because an empty routing table is evidence and a refused
# connection is proof. bash's /dev/tcp needs no curl.
if timeout 5 bash -c 'exec 3<>/dev/tcp/140.82.121.4/443' 2>/dev/null; then
    fail "outbound TCP is refused" "connected to github.com:443 — the container has egress"
else
    pass "outbound TCP is refused" "github.com:443 unreachable"
fi

if timeout 5 getent hosts github.com >/dev/null 2>&1; then
    fail "DNS does not resolve" "github.com resolved"
else
    pass "DNS does not resolve" "github.com does not resolve"
fi

# --------------------------------------------------------------------------
section "nothing is pre-installed for carto to lean on"

# The engine is frozen, so none of these may be needed. If one is present the
# assertion is worthless rather than wrong — it could be silently satisfying an
# import — which is why absence is asserted rather than assumed.
for runtime in python python3 pip pip3 node npm; do
    if command -v "$runtime" >/dev/null 2>&1; then
        fail "no $runtime on the machine" "found at $(command -v "$runtime")"
    else
        pass "no $runtime on the machine"
    fi
done

# --------------------------------------------------------------------------
section "the payload"

PAYLOAD=$(find /opt/payload -maxdepth 2 -name PAYLOAD.json -printf '%h\n' -quit)
if [ -z "$PAYLOAD" ]; then
    fail "payload was built" "no PAYLOAD.json under /opt/payload"
    printf '\nnothing further can run without a payload.\n'
    exit 1
fi
pass "payload was built" "$(basename "$PAYLOAD")"

grammar_libs=$(find "$PAYLOAD/grammars" -name '*.so' 2>/dev/null | wc -l)
if [ "$grammar_libs" -gt 0 ]; then
    pass "grammars are seeded in the payload" "$grammar_libs shared libraries"
else
    fail "grammars are seeded in the payload" "no .so files under grammars/"
fi

# --------------------------------------------------------------------------
section "install"

git config --global user.email acceptance@example.invalid
git config --global user.name "Acceptance Test"
git config --global init.defaultBranch main

mkdir -p "$REPO"
cp -R /opt/fixture/. "$REPO/"
git -C "$REPO" init -q
git -C "$REPO" add -A
git -C "$REPO" commit -qm "fixture"

if /opt/installer/install.sh --payload "$PAYLOAD" --repo "$REPO" >>"$LOG" 2>&1; then
    pass "install.sh completed"
else
    fail "install.sh completed" "exit non-zero; see stderr log below"
fi

export PATH="$CARTO_HOME/bin:$PATH"

# Both names, because the hook lines guard on one and invoke the other.
# Shipping one without the other turns every hook into a silent no-op.
for name in carto cartograph; do
    if command -v "$name" >/dev/null 2>&1; then
        pass "$name resolves on PATH" "$(command -v "$name")"
    else
        fail "$name resolves on PATH" "not found"
    fi
done

# stderr is reported inline rather than only logged: the way this fails is a
# dynamic-linker complaint about a glibc symbol version, and that message is
# the diagnosis. "carto --version failed" on its own sends the reader hunting.
version_err=$(mktemp)
if version=$(carto --version 2>"$version_err"); then
    pass "the frozen engine runs" "$version"
else
    fail "the frozen engine runs" "$(head -1 "$version_err")"
fi
cat "$version_err" >>"$LOG"

# --------------------------------------------------------------------------
section "the query protocol"

run_env carto status --repo "$REPO" --format json
if [ "$RC" -ne 2 ]; then
    fail "status on a fresh repo is a precondition" "exit $RC, expected 2"
elif ! jqok '.ok == false'; then
    fail "status on a fresh repo is a precondition" "ok is not false"
elif ! jqok '.error.code == "precondition"'; then
    fail "status on a fresh repo is a precondition" "error.code is $(jqv '.error.code')"
elif ! jqok '.error.remediation | type == "string" and length > 0'; then
    fail "status on a fresh repo is a precondition" "no remediation to recover with"
else
    pass "status on a fresh repo is a precondition" "remediation: $(jqv '.error.remediation')"
fi

# The assertion this whole harness exists for. A build with no possible egress
# must still parse, which it can only do from the grammars in the payload.
if carto build --repo "$REPO" >>"$LOG" 2>&1; then
    pass "build succeeds with no network"
else
    fail "build succeeds with no network" "carto build exited non-zero"
fi

run_env carto status --repo "$REPO" --format json
if assert_ok_envelope "status reports a graph"; then
    nodes=$(jqv '.data.nodes')
    edges=$(jqv '.data.edges')
    files=$(jqv '.data.files')
    langs=$(jqv '.data.languages | length')
    # > 0, not >= 0: the failure mode being hunted is a build that exits 0
    # having parsed nothing, because every grammar lookup quietly missed.
    if [ "$nodes" -gt 0 ] 2>/dev/null; then
        pass "the graph is NOT empty" "$nodes nodes, $edges edges, $langs languages"
    else
        fail "the graph is NOT empty" "node count is $nodes — grammars did not load"
    fi
    # One grammar could be a fluke of a fallback path. Sixteen distinct ones
    # loading is the seeded cache working.
    if [ "$langs" -ge 10 ] 2>/dev/null; then
        pass "many grammars loaded from the payload" "$langs languages: $(jqv '.data.languages | join(", ")')"
    else
        fail "many grammars loaded from the payload" "only $langs languages parsed"
    fi
fi

run_env carto query callers_of apply_discount --repo "$REPO" --format json
if assert_ok_envelope "query returns a valid envelope"; then
    if jqok '.data.results | type == "array" and length > 0'; then
        pass "query returns a valid envelope" "$(jqv '.data.result_count') result(s) for callers_of"
    else
        fail "query returns a valid envelope" "no results for a call the fixture makes"
    fi
fi

run_env carto search order --repo "$REPO" --format json
if assert_ok_envelope "search returns a valid envelope"; then
    # search_mode is required on search-like results: an agent must never
    # mistake a lexical fallback for semantic search.
    if jqok '.search_mode | type == "string"'; then
        pass "search returns a valid envelope" "$(jqv '.data.results | length') hit(s), search_mode=$(jqv '.search_mode')"
    else
        fail "search returns a valid envelope" "search_mode is absent"
    fi
fi

# review-summary reads a change set, so give it one.
printf '\n\ndef refund_total(items, rate):\n    return -order_total(items, rate)\n' >>"$REPO/src/orders.py"

run_env carto review-summary --repo "$REPO" --format json
if assert_ok_envelope "review-summary returns a valid envelope"; then
    if jqok '.data.changed_file_count >= 1'; then
        pass "review-summary returns a valid envelope" "risk=$(jqv '.data.risk'), $(jqv '.data.changed_file_count') changed file(s)"
    else
        fail "review-summary returns a valid envelope" "changed_file_count is $(jqv '.data.changed_file_count'), expected >= 1"
    fi
fi

run_env carto capabilities --format json
if assert_ok_envelope "capabilities is the catalogue"; then
    if jqok '.data.commands | type == "array" and length > 0'; then
        pass "capabilities is the catalogue" "$(jqv '.data.commands | length') commands"
    else
        fail "capabilities is the catalogue" "the command list is empty"
    fi
fi

# --------------------------------------------------------------------------
section "memory"

MEM_TITLE="Acceptance run recorded this observation"
run_env carto mem add --repo "$REPO" --title "$MEM_TITLE" \
    --body "The grammar cache was seeded at build time, so this ran with no egress." \
    --kind decision --summary-source verbatim --format json
obs_id=""
if assert_ok_envelope "mem add records an observation"; then
    obs_id=$(jqv '.data.observation.id')
    if [ -n "$obs_id" ] && [ "$obs_id" != "null" ]; then
        pass "mem add records an observation" "id $obs_id"
    else
        fail "mem add records an observation" "no id in the envelope"
    fi
fi

run_env carto mem search --repo "$REPO" --query "grammar cache" --limit 5 --format json
if assert_ok_envelope "mem search returns what was added"; then
    # By id, not by title: matching the prose would pass on a store that
    # returned some other observation whose text happened to overlap.
    if [ -n "$obs_id" ] && printf '%s' "$ENV_JSON" |
        jq -e --arg id "$obs_id" '[.data.items[].id] | index($id) != null' >/dev/null 2>&1; then
        pass "mem search returns what was added" "found $obs_id among $(jqv '.data.items | length')"
    else
        fail "mem search returns what was added" "observation $obs_id is not in the results"
    fi
fi

# --------------------------------------------------------------------------
section "the skills pack reaches Copilot"

# The engine writes these from package data frozen into the binary. Comparing
# them against skills/ therefore checks the payload, not this directory.
host=.github/skills
if [ ! -d "$REPO/$host" ]; then
    fail "skills present in $host" "directory was not created"
elif diff -r /opt/canonical-skills "$REPO/$host" >/dev/null 2>&1; then
    n=$(find "$REPO/$host" -name SKILL.md | wc -l)
    pass "skills present in $host" "$n skills, byte-identical to skills/"
else
    fail "skills present in $host" "differs from skills/: $(diff -rq /opt/canonical-skills "$REPO/$host" 2>&1 | head -3 | tr '\n' ';')"
fi

# --------------------------------------------------------------------------
section "the hook a host will actually run"

HOOKS=$REPO/.github/hooks/cartograph.json
HOOK_PROMPT="Acceptance prompt captured through the Copilot hook"
if [ ! -f "$HOOKS" ]; then
    fail "UserPromptSubmit hook records the prompt" "no .github/hooks/cartograph.json"
else
    hook_cmd=$(jq -r '.hooks.UserPromptSubmit[0].command // empty' "$HOOKS")
    if [ -z "$hook_cmd" ]; then
        fail "UserPromptSubmit hook records the prompt" "no UserPromptSubmit command in the hook file"
    else
        # Verbatim, from the repo, with the payload a Copilot host pipes in.
        # A hook that cannot do its job says nothing and exits 0, so the store
        # is the only place to see whether it worked.
        payload=$(jq -cn --arg p "$HOOK_PROMPT" --arg cwd "$REPO" \
            '{session_id: "acceptance-hook", prompt: $p, cwd: $cwd, hook_event_name: "UserPromptSubmit"}')
        hook_out=$(cd "$REPO" && printf '%s' "$payload" | bash -c "$hook_cmd" 2>>"$LOG")
        hook_rc=$?
        run_env carto mem search --repo "$REPO" --query "$HOOK_PROMPT" \
            --session acceptance-hook --doc-type prompts --format json
        if [ "$hook_rc" -ne 0 ]; then
            fail "UserPromptSubmit hook records the prompt" "exit $hook_rc; a hook must never exit non-zero"
        elif [ -n "$hook_out" ]; then
            # Whatever this event prints is prepended to the person's prompt.
            fail "UserPromptSubmit hook records the prompt" "printed '${hook_out}'; it must be silent"
        elif jqok '.data.items | length == 1'; then
            pass "UserPromptSubmit hook records the prompt" "one prompt row in session acceptance-hook"
        else
            fail "UserPromptSubmit hook records the prompt" "expected one prompt row, got $(jqv '.data.items | length')"
        fi
    fi
fi

# --------------------------------------------------------------------------
section "no MCP anywhere"

# The whole reason this toolset exists is that MCP servers are banned. Searched
# by name across everything the install touched, including the payload tree, so
# a stray fastmcp module frozen into the binary would show up here too.
mcp_hits=$(find "$HOME" "$REPO" -iname '*mcp*' 2>/dev/null)
if [ -z "$mcp_hits" ]; then
    pass "zero files matching *mcp*" "searched \$HOME and the repo"
else
    fail "zero files matching *mcp*" "$(printf '%s' "$mcp_hits" | tr '\n' ' ')"
fi

# --------------------------------------------------------------------------
printf '\n'
if [ "$failures" -eq 0 ]; then
    printf 'ALL ASSERTIONS PASSED\n\n'
    printf 'Proved, on a machine with no network, no python, no pip and no node:\n'
    printf '  - install.sh installs the payload and downloads nothing\n'
    printf '  - carto build parsed %s files in %s languages from the seeded grammar cache\n' \
        "${files:-?}" "${langs:-?}"
    printf '  - the graph is non-empty (%s nodes), so no grammar lookup silently missed\n' "${nodes:-?}"
    printf '  - every command answers with a contract-v1 envelope\n'
    printf '  - the skills pack is byte-identical in .github/skills\n'
    printf '  - the generated UserPromptSubmit hook line runs and records the prompt\n'
    printf '  - no MCP configuration is written anywhere\n'
    exit 0
fi

printf '%s FAILED ASSERTION(S)\n\n' "$failures"
for name in "${failed_names[@]}"; do
    printf '  - %s\n' "$name"
done
printf '\n--- stderr from the run ---\n'
tail -60 "$LOG"
exit 1
