#!/usr/bin/env python3
"""Cartograph skills — conformance check.

Skills are the layer that makes a CLI reachable by an agent, which means every
command in a skill body is an instruction a model will follow literally. A
skill that names a flag the CLI does not have does not degrade gracefully: the
agent runs it, gets a usage error, and falls back to reading files by hand —
the exact cost the tool exists to remove.

So skills are checked the same way the envelope is: against the shipped
artifact, never against intent. Every `carto ...` line in a skill body is
extracted and validated against `carto capabilities`, which is itself generated
from the live argparse parser. Skill -> capabilities -> parser, with no
hand-maintained link anywhere in the chain.

    python contracts/capability-v1/check_skills.py --skills skills \\
        --command engine/.venv/bin/python -m cartograph

It shells out; it must never import product code.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

#: `capabilities` is deliberately absent from its own catalogue (listing it
#: would spend an agent's context on the command it is already running), but
#: skills are expected to point at it as the reference layer.
ALWAYS_VALID = {"capabilities"}

#: The binary's own name shows up in prose ("every other `carto` skill"), which
#: is not a cross-reference to a skill called "carto".
NOT_A_SKILL = {"carto"}

#: A placeholder stands for a value the agent supplies. Its *position* is
#: checked, its content is not.
PLACEHOLDER = re.compile(r"^<.+>$|^\{.+\}$")

#: Flags every agent-facing command inherits, or that argparse provides.
UNIVERSAL_FLAGS = {"--help", "-h"}


class Result:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        (self.passed if condition else self.failed).append(
            name if condition else (name, detail)
        )


def load_catalogue(base: list[str]) -> dict:
    proc = subprocess.run(
        base + ["capabilities", "--format", "json"],
        capture_output=True, text=True, timeout=60,
    )
    return json.loads(proc.stdout)["data"]


def load_command(base: list[str], name: str) -> dict | None:
    proc = subprocess.run(
        base + ["capabilities", "--command", name, "--format", "json"],
        capture_output=True, text=True, timeout=60,
    )
    try:
        doc = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    if not doc.get("ok"):
        return None
    return doc["data"]["commands"][0]


def parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out: dict[str, str] = {}
    for line in text[3:end].splitlines():
        if ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
    return out


def extract_commands(text: str) -> list[tuple[int, str]]:
    """Every `carto ...` invocation in a skill body, with its line number.

    Covers fenced blocks (what an agent copies) and inline backticks (what it
    reads mid-sentence). Both are instructions; both get checked.
    """
    found: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line.startswith("carto "):
            found.append((lineno, line))
            continue
        for snippet in re.findall(r"`([^`]+)`", raw):
            snippet = snippet.strip()
            if snippet.startswith("carto "):
                found.append((lineno, snippet))
    return found


def check_invocation(
    base: list[str], tag: str, lineno: int, line: str, catalogue: dict,
    cache: dict, res: Result,
) -> None:
    label = f"{tag}:{lineno} `{line}`"
    try:
        tokens = shlex.split(line)
    except ValueError:
        res.check(f"{label}: parses as a shell command", False, "unbalanced quotes")
        return
    if len(tokens) < 2:
        res.check(f"{label}: names a subcommand", False)
        return
    sub = tokens[1]

    known = {c["name"] for c in catalogue["commands"]} | ALWAYS_VALID
    if sub not in known:
        res.check(f"{label}: `{sub}` is a real command", False,
                  f"not in capabilities; known: {', '.join(sorted(known))}")
        return
    res.check(f"{label}: `{sub}` is a real command", True)
    if sub in ALWAYS_VALID:
        return
    # `carto query` mid-sentence is prose naming the command, not an
    # instruction to run it. Checking it for missing positionals would fail
    # every skill that explains itself in English.
    if len(tokens) == 2:
        return

    if sub not in cache:
        cache[sub] = load_command(base, sub)
    spec = cache[sub]
    if spec is None:
        res.check(f"{label}: capabilities describes `{sub}`", False)
        return

    valid_flags = {f["flag"] for f in spec.get("flags", []) if f.get("flag")}
    choices_for = {
        f["name"]: set(map(str, f["choices"]))
        for f in spec.get("flags", []) if f.get("choices")
    }
    flag_name = {f["flag"]: f["name"] for f in spec.get("flags", []) if f.get("flag")}
    positionals = spec.get("arguments", [])

    seen_positional = 0
    i = 2
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            flag, _, inline = tok.partition("=")
            if flag in UNIVERSAL_FLAGS:
                i += 1
                continue
            ok = flag in valid_flags
            res.check(f"{label}: flag `{flag}` exists on `{sub}`", ok,
                      "" if ok else f"valid: {' '.join(sorted(valid_flags))}")
            # Consume the flag's value so it is not mistaken for a positional.
            value = inline or (tokens[i + 1] if i + 1 < len(tokens)
                               and not tokens[i + 1].startswith("--") else None)
            if ok and value and not PLACEHOLDER.match(value):
                allowed = choices_for.get(flag_name.get(flag, ""))
                if allowed:
                    res.check(f"{label}: `{flag} {value}` is an allowed value",
                              value in allowed,
                              f"allowed: {' '.join(sorted(allowed))}")
            i += 1 if inline or value is None else 2
            continue
        # A positional.
        if seen_positional < len(positionals):
            spec_pos = positionals[seen_positional]
            allowed = set(map(str, spec_pos["choices"])) if spec_pos.get("choices") else None
            if allowed and not PLACEHOLDER.match(tok):
                res.check(f"{label}: `{tok}` is a valid {spec_pos['name']}",
                          tok in allowed,
                          f"allowed: {' '.join(sorted(allowed))}")
        seen_positional += 1
        i += 1

    required = [p for p in positionals if p.get("required")]
    res.check(f"{label}: supplies {len(required)} positional argument(s)",
              seen_positional >= len(required),
              f"got {seen_positional}, needs {[p['name'] for p in required]}")
    # Too many is as wrong as too few, and fails more confusingly: `carto
    # impact <node>` looks reasonable, but impact takes --files, so argparse
    # rejects a call the agent had every reason to believe in.
    res.check(f"{label}: passes no positional `{sub}` does not take",
              seen_positional <= len(positionals),
              f"got {seen_positional} positional(s), `{sub}` takes "
              f"{len(positionals)}: {[p['name'] for p in positionals]}")


def check_skill(base: list[str], path: Path, catalogue: dict, cache: dict,
                res: Result, names: set[str]) -> None:
    tag = path.parent.name
    text = path.read_text()
    fm = parse_frontmatter(text)

    res.check(f"{tag}: has frontmatter", bool(fm))
    res.check(f"{tag}: frontmatter name matches directory",
              fm.get("name") == tag, f"name={fm.get('name')!r}")
    desc = fm.get("description", "")
    # The description is the only thing a host reads when deciding whether to
    # load the skill at all, so an unhelpful one costs more than a missing body.
    res.check(f"{tag}: has a description", bool(desc))
    res.check(f"{tag}: description fits the 1024-char host limit", len(desc) <= 1024,
              f"{len(desc)} chars")
    res.check(f"{tag}: description says when NOT to use the skill",
              "not for" in desc.lower() or "not to" in desc.lower(),
              "without it, hosts load the wrong skill and pay for its context")
    names.add(tag)

    invocations = extract_commands(text)
    res.check(f"{tag}: contains at least one carto invocation", bool(invocations))
    for lineno, line in invocations:
        check_invocation(base, tag, lineno, line, catalogue, cache, res)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--skills", required=True, type=Path)
    ap.add_argument("--command", required=True, nargs=argparse.REMAINDER,
                    help="How to invoke the CLI under test (everything after this flag)")
    args = ap.parse_args()

    base = args.command
    catalogue = load_catalogue(base)
    res, cache, names = Result(), {}, set()

    skills = sorted(args.skills.glob("*/SKILL.md"))
    if not skills:
        print(f"no skills found under {args.skills}/*/SKILL.md")
        return 1
    for path in skills:
        check_skill(base, path, catalogue, cache, res, names)

    # Cross-references must resolve, or the agent is sent to a skill that does
    # not exist and silently gives up on the workflow.
    for path in skills:
        text = path.read_text()
        for ref in re.findall(r"`([a-z-]+)` skill|use the `?([a-z-]+)`? skill", text):
            for candidate in filter(None, ref):
                if (candidate in names or candidate == path.parent.name
                        or candidate in NOT_A_SKILL):
                    continue
                res.check(f"{path.parent.name}: cross-reference `{candidate}` resolves",
                          False, f"known skills: {', '.join(sorted(names))}")

    for label in res.passed:
        print(f"  ok    {label}")
    for label, detail in res.failed:
        print(f"  FAIL  {label}" + (f"\n          {detail}" if detail else ""))
    total = len(res.passed) + len(res.failed)
    print(f"\nskills: {len(res.passed)}/{total} checks passed ({len(skills)} skills)")
    return 1 if res.failed else 0


if __name__ == "__main__":
    sys.exit(main())
