"""`carto review-context` must not spend an agent's context on absolute paths."""

from cartograph.review_shape import shape_review_context

REPO = "/repo"


def _raw() -> dict:
    return {
        "summary": "s",
        "context": {
            "changed_files": ["/repo/pkg/cli.py"],
            "impacted_files": ["/repo/pkg/other.py"],
            "graph": {
                "changed_nodes": [
                    {
                        "kind": "File",
                        "name": "/repo/pkg/cli.py",
                        "qualified_name": "/repo/pkg/cli.py",
                        "file_path": "/repo/pkg/cli.py",
                    },
                    {
                        "kind": "Function",
                        "name": "main",
                        "qualified_name": "/repo/pkg/cli.py::main",
                        "file_path": "/repo/pkg/cli.py",
                    },
                ],
                "impacted_nodes": [
                    {
                        "kind": "Class",
                        "name": "Outer.Inner",
                        "qualified_name": "/repo/pkg/cli.py::Outer::Inner",
                        "file_path": "/repo/pkg/cli.py",
                    },
                ],
                "edges": [
                    {
                        "kind": "CONTAINS",
                        "source": "/repo/pkg/cli.py",
                        "target": "/repo/pkg/cli.py::main",
                        "file_path": "/repo/pkg/cli.py",
                    },
                ],
            },
        },
    }


def test_ids_and_titles_are_repo_relative():
    shaped = shape_review_context(_raw(), repo_root=REPO)
    nodes = shaped["facets"]["changed_nodes"]["items"]
    assert [n["id"] for n in nodes] == ["pkg/cli.py", "pkg/cli.py::main"]
    # A file node's title is its leaf, not the path `location.file` already has.
    assert [n["title"] for n in nodes] == ["cli.py", "main"]
    assert nodes[0]["location"]["file"] == "pkg/cli.py"


def test_symbol_suffix_survives_relativisation():
    """The `::` suffix is what makes an id resolvable; only the path shortens."""
    shaped = shape_review_context(_raw(), repo_root=REPO)
    assert shaped["items"][0]["id"] == "pkg/cli.py::Outer::Inner"


def test_edge_endpoints_are_relativised():
    edge = shape_review_context(_raw(), repo_root=REPO)["facets"]["edges"]["items"][0]
    assert edge["source"] == "pkg/cli.py"
    assert edge["target"] == "pkg/cli.py::main"


def test_paths_outside_the_repo_are_left_alone():
    """Relativising is a courtesy, not a guarantee — an outside path stays whole."""
    raw = _raw()
    raw["context"]["graph"]["changed_nodes"] = [
        {
            "kind": "Function",
            "name": "helper",
            "qualified_name": "/elsewhere/lib.py::helper",
            "file_path": "/elsewhere/lib.py",
        },
    ]
    node = shape_review_context(raw, repo_root=REPO)["facets"]["changed_nodes"]["items"][0]
    assert node["id"] == "/elsewhere/lib.py::helper"


def test_no_repo_root_leaves_names_untouched():
    shaped = shape_review_context(_raw(), repo_root=None)
    assert shaped["facets"]["changed_nodes"]["items"][1]["id"] == "/repo/pkg/cli.py::main"
