"""`carto architecture` says what the repository is made of, not only its code.

A measured Copilot run asked "how is this repository structured" of a kit that
is mostly documentation and templates; from `architecture` alone it described
the few Python scripts as the repository, five times out of five. These pin
the layout section that answers that: every working-tree file, by top-level
directory and kind, and a note when most of it is not code.
"""

from __future__ import annotations

import json
import subprocess
import sys
from unittest.mock import patch

import pytest

from cartograph import cli, compact, layout


def _docs_heavy_files() -> list[str]:
    return (
        ["README.md", "AGENTS.md", "LICENSE", "requirements.txt"]
        + [f"docs/guide-{i}.md" for i in range(20)]
        + [f"template/docs/t{i}.md" for i in range(10)]
        + ["template/README.md.tmpl", ".github/workflows/ci.yml"]
        + ["scripts/ingest.py", "scripts/store.py", "dashboard/app.js"]
    )


def test_kinds_are_decided_by_name():
    assert layout.kind_of("docs/a.md") == "docs"
    assert layout.kind_of("LICENSE") == "docs"
    assert layout.kind_of("requirements.txt") == "config"
    assert layout.kind_of(".github/workflows/ci.yml") == "config"
    assert layout.kind_of(".gitignore") == "config"
    assert layout.kind_of("src/a.py") == "code"
    assert layout.kind_of("web/index.html") == "code"
    assert layout.kind_of("img/logo.png") == "other"


def test_docs_heavy_repository_says_the_graph_is_a_small_part():
    files = _docs_heavy_files()
    out = layout.summarise(files, {"scripts/ingest.py", "scripts/store.py"})
    assert out["files"] == 39
    assert out["by_kind"] == {"code": 3, "docs": 33, "config": 2, "other": 1}
    assert out["graph_files"] == 2
    root, *dirs = out["dirs"]
    assert root == {"dir": "(root)", "files": 4, "kinds": {"docs": 3, "config": 1},
                    "notable": ["README.md", "AGENTS.md"]}
    assert [d["dir"] for d in dirs] == [
        "docs/", "template/", "scripts/", ".github/", "dashboard/",
    ]
    assert out["note"] == (
        "92% of files are not code; the code graph covers 5%. "
        "Communities below describe only that part; read README.md, AGENTS.md "
        "for the rest."
    )


def test_code_heavy_repository_has_no_note():
    files = [f"src/m{i}.ts" for i in range(30)] + ["README.md", "package.json"]
    out = layout.summarise(files, set(files[:30]))
    assert "note" not in out
    assert out["graph_files"] == 30


def test_many_directories_are_summed_not_listed():
    files = [f"d{i:02}/f.py" for i in range(layout.MAX_DIRS + 3)]
    out = layout.summarise(files, set())
    assert len(out["dirs"]) == layout.MAX_DIRS
    assert out["dirs_omitted"] == {"dirs": 3, "files": 3}


def test_layout_rows_are_compact():
    assert compact.layout_dir_row(
        {"dir": "(root)", "files": 4, "kinds": {"docs": 3, "config": 1},
         "notable": ["README.md", "AGENTS.md"]}
    ) == "(root) | 4 files | docs 3, config 1 | read first: README.md, AGENTS.md"
    assert compact.layout_dir_row(
        {"dir": "docs/", "files": 20, "kinds": {"docs": 20}}
    ) == "docs/ | 20 files | docs 20"


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   stdin=subprocess.DEVNULL)


@pytest.fixture
def docs_kit(tmp_path):
    """A small repository shaped like the one in the A/B: mostly markdown and
    templates, a few Python scripts, a graph built over them."""
    repo = tmp_path / "kit"
    for rel in _docs_heavy_files():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# x\n", encoding="utf-8")
    (repo / "scripts/ingest.py").write_text(
        "from store import save\n\ndef ingest(p):\n    return save(p)\n",
        encoding="utf-8",
    )
    (repo / "scripts/store.py").write_text(
        "def save(p):\n    return p\n", encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x")
    from cartograph.graph import GraphStore
    from cartograph.incremental import full_build, get_db_path

    store = GraphStore(get_db_path(repo))
    try:
        full_build(repo, store)
    finally:
        store.close()
    return repo


def test_architecture_leads_with_the_layout(docs_kit, capsys):
    argv = ["cartograph", "architecture", "--repo", str(docs_kit), "--format", "json"]
    with patch.object(sys, "argv", argv):
        try:
            cli.main()
        except SystemExit as exit_:
            assert exit_.code in (0, None)
    data = json.loads(capsys.readouterr().out)["data"]
    assert list(data)[:2] == ["summary", "layout"]
    found = data["layout"]
    assert found["files"] == 39
    assert found["dirs"][0] == (
        "(root) | 4 files | docs 3, config 1 | read first: README.md, AGENTS.md"
    )
    assert "docs/ | 20 files | docs 20" in found["dirs"]
    assert found["note"].startswith("92% of files are not code")
    # The graph parsed the code and nothing else: two scripts, the JS file,
    # and the workflow YAML at most.
    assert 2 <= found["graph_files"] <= 4
