"""No agent-facing response should spend context on this checkout's prefix.

`review-context` has its own shaper (test_review_shape_paths.py); every other
graph command shares the CLI emit path, which applies the same shortening
through `repo_paths.relativise_result`.
"""

from cartograph.repo_paths import (
    relativise,
    relativise_qualified,
    relativise_result,
)

REPO = "/repo"


def test_plain_paths_become_repo_relative():
    assert relativise("/repo/pkg/cli.py", REPO) == "pkg/cli.py"


def test_qualified_names_keep_their_symbol_half():
    assert relativise_qualified("/repo/pkg/cli.py::main", REPO) == "pkg/cli.py::main"


def test_nested_symbols_survive_whole():
    """Partition on the FIRST `::`, or a nested symbol loses its tail."""
    assert (
        relativise_qualified("/repo/pkg/cli.py::Outer::Inner", REPO)
        == "pkg/cli.py::Outer::Inner"
    )


def test_separators_inside_a_symbol_are_not_normalised():
    """A PHP namespace identifier legitimately contains backslashes."""
    name = "/repo/src/Job.php::App\\Domain\\Job"
    assert relativise_qualified(name, REPO) == "src/Job.php::App\\Domain\\Job"


def test_search_result_rows_are_relativised():
    result = {
        "results": [
            {
                "name": "/repo/pkg/cli.py",
                "qualified_name": "/repo/pkg/cli.py",
                "file_path": "/repo/pkg/cli.py",
                "signature": "/repo/pkg/cli.py",
            },
            {
                "name": "main",
                "qualified_name": "/repo/pkg/cli.py::main",
                "file_path": "/repo/pkg/cli.py",
                "signature": "def main()",
            },
        ],
    }
    relativise_result(result, REPO)
    assert result["results"][0]["qualified_name"] == "pkg/cli.py"
    assert result["results"][0]["signature"] == "pkg/cli.py"
    assert result["results"][1]["qualified_name"] == "pkg/cli.py::main"
    assert result["results"][1]["signature"] == "def main()"


def test_edges_lists_and_nested_collections_are_relativised():
    result = {
        "impacted_files": ["/repo/pkg/other.py"],
        "edges": [
            {
                "source": "/repo/pkg/cli.py",
                "target": "/repo/pkg/cli.py::main",
                "file_path": "/repo/pkg/cli.py",
                "unresolved_targets": ["/repo/pkg/other.py::helper"],
            },
        ],
        "community": {"members": ["/repo/pkg/cli.py::main"]},
    }
    relativise_result(result, REPO)
    assert result["impacted_files"] == ["pkg/other.py"]
    edge = result["edges"][0]
    assert edge["source"] == "pkg/cli.py"
    assert edge["target"] == "pkg/cli.py::main"
    assert edge["unresolved_targets"] == ["pkg/other.py::helper"]
    assert result["community"]["members"] == ["pkg/cli.py::main"]


def test_source_snippets_are_left_alone():
    """Quoted file content may mention an absolute path; rewriting it lies."""
    quoted = "1: DB = '/repo/pkg/graph.db'"
    result = {"facets": {"source_snippets": {"pkg/cli.py": quoted}}}
    relativise_result(result, REPO)
    assert result["facets"]["source_snippets"]["pkg/cli.py"] == quoted


def test_paths_outside_the_repo_are_left_alone():
    assert relativise_qualified("/elsewhere/lib.py::helper", REPO) == (
        "/elsewhere/lib.py::helper"
    )


def test_relativising_is_idempotent():
    """review-context arrives already shaped and must not be shortened twice."""
    result = {"file_path": "pkg/cli.py"}
    relativise_result(result, REPO)
    assert result["file_path"] == "pkg/cli.py"


def test_no_repo_root_leaves_everything_untouched():
    result = {"file_path": "/repo/pkg/cli.py"}
    relativise_result(result, None)
    assert result["file_path"] == "/repo/pkg/cli.py"
