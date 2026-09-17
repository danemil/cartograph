"""`carto mem` end to end: exit codes, the envelope, paging and the catalogue.

Driven through ``cli.main`` with a patched argv rather than a subprocess, so a
failure points at a line rather than at a return code. What is asserted is the
protocol — stdout, exit status, the envelope — never how the store got there.
"""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest
from cartograph import cli


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A project root with no graph and no memory — the first-run state."""
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    (tmp_path / ".git").mkdir()
    return tmp_path


@pytest.fixture
def carto(capsys):
    """Run the CLI once and return ``(exit_code, parsed_stdout, stderr)``."""

    def run(*argv):
        with patch.object(sys, "argv", ["carto", *argv]):
            try:
                cli.main()
                code = 0
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        captured = capsys.readouterr()
        document = None
        if captured.out.strip():
            # Parsing IS the assertion that stdout carried the envelope and
            # nothing else: a stray log line makes this raise.
            document = json.loads(captured.out)
        return code, document, captured.err

    return run


def _add(carto, repo, title, **kwargs):
    argv = ["mem", "add", "--repo", str(repo), "--title", title]
    for flag, value in kwargs.items():
        argv += [f"--{flag.replace('_', '-')}", value]
    code, doc, _ = carto(*argv)
    assert code == 0, doc
    return doc["data"]["observation"]


class TestPreconditions:
    @pytest.mark.parametrize("command", ["search", "status"])
    def test_no_store_is_exit_2_with_the_command_that_fixes_it(
        self, carto, repo, command
    ):
        argv = ["mem", command, "--repo", str(repo)]
        if command == "search":
            argv += ["--query", "anything"]
        code, doc, _ = carto(*argv)
        assert code == 2
        assert doc["ok"] is False
        assert doc["error"]["code"] == "precondition"
        assert doc["error"]["remediation"].startswith("carto mem add")
        # The remediation names a path the agent can act on, not this checkout.
        assert str(repo) not in doc["error"]["message"]

    def test_the_store_is_not_created_by_looking_for_it(self, carto, repo):
        carto("mem", "status", "--repo", str(repo))
        assert not (repo / ".cartograph" / "memory.db").exists()


class TestWriteAndStatus:
    def test_add_then_status(self, carto, repo):
        record = _add(carto, repo, "first observation", body="a body", kind="decision")
        assert record["id"]
        code, doc, _ = carto("mem", "status", "--repo", str(repo))
        assert code == 0
        data = doc["data"]
        assert data["observations"] == 1
        assert data["kinds"] == ["decision"]
        # Repo-relative, like every other agent-facing path in this CLI.
        assert data["store"] == ".cartograph/memory.db"

    def test_status_says_why_search_is_keyword_only(self, carto, repo):
        _add(carto, repo, "an observation")
        _, doc, _ = carto("mem", "status", "--repo", str(repo))
        assert doc["data"]["semantic_search"] is False
        # `search_mode` can only say embeddings did not participate; an agent
        # cannot act on that without knowing which precondition is missing.
        assert doc["data"]["semantic_search_unavailable"]

    def test_status_does_not_page(self, carto, repo):
        _add(carto, repo, "an observation")
        _, doc, _ = carto("mem", "status", "--repo", str(repo))
        assert "page" not in doc

    def test_file_paths_are_stored_repo_relative(self, carto, repo):
        target = repo / "src" / "thing.py"
        target.parent.mkdir()
        target.write_text("x = 1\n")
        record = _add(carto, repo, "about a file", file=str(target))
        assert record["file_paths"] == ["src/thing.py"]


class TestSearch:
    def test_envelope_shape(self, carto, repo):
        _add(carto, repo, "token budget notes", body="semantic truncation")
        code, doc, _ = carto(
            "mem", "search", "--repo", str(repo), "--query", "budget", "--limit", "5"
        )
        assert code == 0
        assert doc["tool"] == "mem search"
        # The store reports "fts"; the contract's vocabulary is "keyword", and
        # the envelope is the one seam that guarantees the translation.
        assert doc["search_mode"] == "keyword"
        assert doc["page"]["limit"] == 5
        assert doc["data"]["items"][0]["title"] == "token budget notes"
        assert doc["provenance"]["built_at"]

    def test_no_match_is_success(self, carto, repo):
        _add(carto, repo, "an observation")
        code, doc, _ = carto(
            "mem", "search", "--repo", str(repo),
            "--query", "zzz_nothing_matches_zzz", "--limit", "7",
        )
        assert code == 0
        assert doc["data"]["items"] == []
        assert doc["page"] == {
            "limit": 7, "has_more": False, "next_cursor": None, "result_count": 0,
        }

    def test_usage_error_is_exit_1(self, carto, repo):
        code, doc, _ = carto(
            "mem", "search", "--repo", str(repo), "--query", "x", "--limit", "0"
        )
        assert code == 1
        assert doc["error"]["code"] == "usage"

    def test_a_namespace_is_not_a_command(self, carto):
        code, doc, _ = carto("mem", "--format", "json")
        assert code == 1
        assert doc["ok"] is False
        assert doc["error"]["code"] == "usage"


class TestPaging:
    def _seed(self, carto, repo, count=6):
        for index in range(count):
            _add(carto, repo, f"paged observation {index}", body="shared word")

    def test_a_cursor_continues_the_same_query(self, carto, repo):
        self._seed(carto, repo)
        args = ["mem", "search", "--repo", str(repo), "--query", "shared", "--limit", "2"]
        _, first, _ = carto(*args)
        assert first["page"]["has_more"] is True
        cursor = first["page"]["next_cursor"]
        assert cursor

        _, second, _ = carto(*args, "--cursor", cursor)
        assert second["ok"] is True
        page_one = [item["id"] for item in first["data"]["items"]]
        page_two = [item["id"] for item in second["data"]["items"]]
        assert not set(page_one) & set(page_two)

        # The two pages must be the single-call sequence, or the fetch-and-
        # discard has slipped a row.
        _, whole, _ = carto(
            "mem", "search", "--repo", str(repo), "--query", "shared", "--limit", "4"
        )
        assert [item["id"] for item in whole["data"]["items"]] == page_one + page_two

    def test_a_write_refuses_an_outstanding_cursor(self, carto, repo):
        self._seed(carto, repo)
        args = ["mem", "search", "--repo", str(repo), "--query", "shared", "--limit", "2"]
        _, first, _ = carto(*args)
        _add(carto, repo, "written after the cursor was issued", body="shared word")

        code, doc, _ = carto(*args, "--cursor", first["page"]["next_cursor"])
        # Loud and recoverable, rather than a page silently spliced from two
        # states of the store.
        assert code == 2
        assert doc["error"]["code"] == "precondition"
        assert doc["error"]["remediation"]

    def test_a_cursor_from_another_query_is_refused(self, carto, repo):
        self._seed(carto, repo)
        _, first, _ = carto(
            "mem", "search", "--repo", str(repo), "--query", "shared", "--limit", "2"
        )
        code, doc, _ = carto(
            "mem", "search", "--repo", str(repo), "--query", "paged", "--limit", "2",
            "--cursor", first["page"]["next_cursor"],
        )
        assert code == 2
        assert "different query" in doc["error"]["message"]


class TestBudget:
    def test_the_budget_is_honoured_and_the_summary_survives(self, carto, repo):
        for index in range(12):
            _add(
                carto, repo, f"budgeted observation {index}",
                body="shared word " * 40,
            )
        _, doc, _ = carto(
            "mem", "search", "--repo", str(repo), "--query", "shared",
            "--limit", "12", "--max-tokens", "400",
        )
        assert doc["size"]["budget_tokens"] == 400
        assert doc["size"]["tokens_estimated"] <= 400 or doc["size"]["over_budget"]
        assert doc["truncated"] is True
        assert doc["truncated_reason"] == "max_tokens"
        # The floor: a response without its summary is not a cheaper answer.
        assert doc["data"]["summary"]

    def test_a_trimmed_page_mints_a_cursor_for_what_was_dropped(self, carto, repo):
        for index in range(12):
            _add(carto, repo, f"budgeted observation {index}", body="shared word " * 40)
        _, doc, _ = carto(
            "mem", "search", "--repo", str(repo), "--query", "shared",
            "--limit", "12", "--max-tokens", "400",
        )
        emitted = len(doc["data"]["items"])
        assert doc["page"]["result_count"] == emitted
        assert doc["page"]["next_cursor"]


class TestCapabilities:
    def test_the_catalogue_lists_the_leaves_not_the_namespace(self, carto):
        _, doc, _ = carto("capabilities")
        names = {command["name"] for command in doc["data"]["commands"]}
        assert {"mem add", "mem search", "mem status"} <= names
        # `carto mem` runs nothing; an agent shown it would call it and get a
        # usage error.
        assert "mem" not in names

    def test_the_drill_down_accepts_the_name_the_listing_emitted(self, carto):
        _, doc, _ = carto("capabilities", "--command", "mem search")
        command = doc["data"]["commands"][0]
        flags = {flag["flag"] for flag in command["flags"]}
        assert command["name"] == "mem search"
        assert {"--query", "--limit", "--cursor", "--max-tokens", "--format"} <= flags

    def test_only_the_pageable_subcommand_advertises_a_cursor(self, carto):
        for name in ("mem add", "mem status"):
            _, doc, _ = carto("capabilities", "--command", name)
            flags = {flag["flag"] for flag in doc["data"]["commands"][0]["flags"]}
            assert "--cursor" not in flags

    def test_a_namespace_is_not_describable(self, carto):
        code, doc, _ = carto("capabilities", "--command", "mem")
        assert code == 1
        assert "mem search" in doc["error"]["message"]
