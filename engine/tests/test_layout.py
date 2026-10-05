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


# --------------------------------------------------------------------------
# A dominant directory names what is inside it
# --------------------------------------------------------------------------
#
# The fifth A/B (report6, T2): on a kit whose implementation sits under
# `template/`, 3 of 5 answers left out the role-playbook skills and the MCP
# server — both nested in `template/` — because the layout named top-level
# directories only, and `template/` read as "docs 11, config 1, other 1".

_ROLES = ("architect", "developer", "tester", "product-owner", "scrum-master",
          "devops", "security", "ux")


def _template_kit_files() -> list[str]:
    """The A/B repository's shape: docs at the top, the implementation in template/."""
    return (
        ["README.md", "AGENTS.md", "CLAUDE.md", "LICENSE", "CHANGELOG.md"]
        + [f"docs/guides/g{i}.md" for i in range(20)]
        + [f"docs/adr/{i:04}.md" for i in range(8)]
        + ["docs/knowledge/graph-manifest.json", "docs/index.md"]
        + [".github/workflows/docs.yml", ".github/workflows/lint.yml"]
        + [f"template/.claude/skills/{r}/SKILL.md" for r in _ROLES]
        + [f"template/.claude/skills/{r}/reference.md" for r in _ROLES]
        + ["template/.claude/hooks/session_start.sh",
           "template/.claude/hooks/session_end.sh",
           "template/.claude/settings.json"]
        + [f"template/scripts/knowledge/{m}.py" for m in (
            "__init__", "graph_store", "ingest", "ingest_code", "ingest_docs",
            "ingest_issues", "link_commits", "link_issues", "query", "manifest",
            "mcp_server")]
        + [f"template/scripts/knowledge/tests/test_{m}.py" for m in (
            "query", "ingest", "end_to_end", "issue_chain", "mcp_server")]
        + [f"template/scripts/validate_{m}.py" for m in ("docs", "links", "adr")]
        + ["template/dashboard/app.py", "template/dashboard/collectors.py",
           "template/dashboard/importers.py", "template/dashboard/README.md"]
        + [f"template/docs/t{i}.md" for i in range(20)]
        + ["template/.github/workflows/ci.yml",
           "template/.github/workflows/validate.yml"]
        + ["template/AGENTS.md", "template/README.md.tmpl"]
    )


def _row(out, name):
    return next(r for r in out["dirs"] if r["dir"] == name)


def test_a_dominant_directory_names_its_sub_directories():
    files = _template_kit_files()
    out = layout.summarise(files, set())
    template = _row(out, "template/")
    assert template["files"] == 66
    # Largest first, with counts and kinds, as the top level is.
    assert template["subdirs"][:3] == [
        {"dir": "template/docs/", "files": 20, "kinds": {"docs": 20}},
        {"dir": "template/.claude/", "files": 19,
         "kinds": {"code": 2, "docs": 16, "config": 1}},
        {"dir": "template/scripts/", "files": 19, "kinds": {"code": 19}},
    ]
    # Every file is accounted for: listed sub-directories, the rest summed,
    # and the files directly in it.
    listed = sum(s["files"] for s in template["subdirs"])
    rest = template.get("subdirs_omitted", {}).get("files", 0)
    assert listed + rest + template["own_files"] == template["files"]


def test_a_dominant_directory_names_its_recognisable_components():
    out = layout.summarise(_template_kit_files(), set())
    parts = _row(out, "template/")["components"]
    assert parts["skills"] == [{"dir": "template/.claude/skills/", "count": 8}]
    # The server is named; its test file is not a server.
    assert parts["servers"] == ["template/scripts/knowledge/mcp_server.py"]
    assert parts["apps"] == ["template/dashboard/"]
    assert parts["tests"] == [{"dir": "template/scripts/knowledge/tests/", "files": 5}]
    assert parts["hooks"] == ["template/.claude/hooks/"]
    assert parts["ci"] == [{"dir": "template/.github/workflows/", "files": 2}]


def test_directories_below_the_threshold_stay_one_line():
    out = layout.summarise(_template_kit_files(), set())
    github = _row(out, ".github/")
    assert 100 * github["files"] / out["files"] < layout.EXPAND_FILE_SHARE
    assert "subdirs" not in github and "components" not in github


def test_a_directory_holding_most_of_the_code_is_expanded_however_small():
    # 12 of 60 files (20%, under the file share), but every code file.
    files = ([f"docs/d{i}.md" for i in range(48)]
             + [f"engine/core/m{i}.py" for i in range(8)]
             + [f"engine/server/api{i}.py" for i in range(4)])
    out = layout.summarise(files, set())
    engine = _row(out, "engine/")
    assert 100 * engine["files"] / out["files"] < layout.EXPAND_FILE_SHARE
    assert [s["dir"] for s in engine["subdirs"]] == ["engine/core/", "engine/server/"]
    assert engine["components"]["servers"] == ["engine/server/"]


def test_the_expanded_row_is_compact():
    out = layout.summarise(_template_kit_files(), set())
    row = compact.layout_dir_row(_row(out, "template/"))
    assert row == (
        "template/ | 66 files | code 24, docs 38, config 3, other 1"
        " | sub: docs/ 20 (docs 20), .claude/ 19 (code 2, docs 16, config 1),"
        " scripts/ 19 (code 19), dashboard/ 4 (code 3, docs 1),"
        " .github/ 2 (config 2); 2 own files"
        " | skills: template/.claude/skills/ (8)"
        "; server: template/scripts/knowledge/mcp_server.py"
        "; app: template/dashboard/"
        "; tests: template/scripts/knowledge/tests/ (5)"
        "; hooks: template/.claude/hooks/"
        "; ci: template/.github/workflows/ (2)"
    )


@pytest.fixture
def template_kit(tmp_path):
    repo = tmp_path / "kit"
    for rel in _template_kit_files():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# x\n", encoding="utf-8")
    (repo / "template/scripts/knowledge/graph_store.py").write_text(
        "def add_node(store, name):\n    return name\n", encoding="utf-8",
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


def test_architecture_names_the_components_inside_a_dominant_directory(template_kit, capsys):
    argv = ["cartograph", "architecture", "--repo", str(template_kit), "--format", "json"]
    with patch.object(sys, "argv", argv):
        try:
            cli.main()
        except SystemExit as exit_:
            assert exit_.code in (0, None)
    rows = json.loads(capsys.readouterr().out)["data"]["layout"]["dirs"]
    template = next(r for r in rows if r.startswith("template/ |"))
    for part in ("skills: template/.claude/skills/ (8)",
                 "server: template/scripts/knowledge/mcp_server.py",
                 "app: template/dashboard/",
                 "tests: template/scripts/knowledge/tests/ (5)"):
        assert part in template, part
    assert not any(r.startswith(".github/ |") and "sub:" in r for r in rows)


def test_a_test_tree_is_not_expanded_and_holds_no_servers():
    # claude-mem's tests/ holds 35% of its files, with tests/server/ and
    # tests/ui/ inside: naming those as a server and an app would be wrong.
    files = ([f"tests/server/t{i}.ts" for i in range(30)]
             + [f"tests/ui/t{i}.ts" for i in range(10)]
             + [f"src/m{i}.ts" for i in range(20)]
             + [f"lib/x/tests/server/t{i}.ts" for i in range(3)]
             + [f"lib/x/m{i}.ts" for i in range(40)])
    out = layout.summarise(files, set())
    assert "subdirs" not in _row(out, "tests/")
    lib = _row(out, "lib/")
    assert lib["components"] == {"tests": [{"dir": "lib/x/tests/", "files": 3}]}
