"""`build` and `update` speak the envelope with `--format json`.

The README promises `--format json|text` on every command. A user's A/B on
0.8.8 ran `carto update --format json` and got a usage error instead. These
run the CLI as a subprocess, because the promise is about what reaches stdout
and stderr, and an in-process capture would hide a stray print.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
SCHEMA = json.loads(
    (ENGINE.parent / "contracts" / "capability-v1" / "schemas" / "envelope.schema.json")
    .read_text()
)


def _carto(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(ENGINE)}
    return subprocess.run(
        [sys.executable, "-m", "cartograph", *args],
        cwd=cwd, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
        timeout=120,
    )


def _envelope(proc: subprocess.CompletedProcess) -> dict:
    from jsonschema import Draft202012Validator

    doc = json.loads(proc.stdout)  # stdout is the envelope and nothing else
    errors = [e.message for e in Draft202012Validator(SCHEMA).iter_errors(doc)]
    assert not errors, errors
    return doc


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   stdin=subprocess.DEVNULL)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "a.py").write_text("def f():\n    return 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def test_build_json_is_one_envelope(repo):
    proc = _carto("build", "--repo", str(repo), "--format", "json", cwd=repo)
    assert proc.returncode == 0, proc.stderr
    doc = _envelope(proc)
    assert doc["ok"] is True and doc["tool"] == "build"
    data = doc["data"]
    assert data["build_type"] == "full"
    assert data["files_parsed"] == 1
    assert data["total_nodes"] >= 2
    assert data["summary"].startswith("Full build")


def test_update_json_with_nothing_to_do(repo):
    _carto("build", "--repo", str(repo), cwd=repo)
    proc = _carto("update", "--repo", str(repo), "--format", "json", cwd=repo)
    assert proc.returncode == 0, proc.stderr
    doc = _envelope(proc)
    assert doc["tool"] == "update"
    assert doc["data"]["files_updated"] == 0
    assert "up to date" in doc["data"]["summary"]
    # total_* is the whole graph for update as for build; an agent read
    # "total_nodes: 0" on an up-to-date graph as an empty one.
    assert doc["data"]["total_nodes"] >= 2
    assert doc["data"]["total_edges"] >= 1
    assert doc["data"]["nodes_updated"] == 0


def test_update_json_names_changed_files_repo_relative(repo):
    _carto("build", "--repo", str(repo), cwd=repo)
    (repo / "a.py").write_text("def f():\n    return 2\n\n\ndef g():\n    return f()\n")
    proc = _carto("update", "--repo", str(repo), "--format", "json", cwd=repo)
    assert proc.returncode == 0, proc.stderr
    data = _envelope(proc)["data"]
    assert data["files_updated"] >= 1
    assert data["changed_files"] == ["a.py"]
    assert str(repo) not in json.dumps(data)
    assert data["nodes_updated"] >= 3          # a.py's file node, f and g
    assert data["total_nodes"] >= data["nodes_updated"]


def test_update_json_outside_git_is_a_precondition(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1\n")
    # No --repo: a plain directory outside any repository.
    proc = _carto("update", "--format", "json", cwd=plain)
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    doc = _envelope(proc)
    assert doc["error"]["code"] == "precondition"
    assert "carto build" in doc["error"]["remediation"]


def test_update_json_on_another_repositorys_graph_is_a_precondition(repo, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    for name in ("x.py", "y.py", "z.py"):
        (other / name).write_text("def h():\n    return 0\n")
    _carto("build", "--repo", str(other), cwd=other)
    _carto("build", "--repo", str(repo), cwd=repo)
    shutil.copy2(other / ".cartograph" / "graph.db", repo / ".cartograph" / "graph.db")
    # An explicit base keeps it incremental; without one, a graph whose commit
    # is not in this history falls back to a full rebuild, which is the remedy.
    proc = _carto("update", "--repo", str(repo), "--base", "HEAD", "--format", "json",
                  cwd=repo)
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    doc = _envelope(proc)
    assert doc["error"]["code"] == "precondition"
    assert "different repository root" in doc["error"]["message"]


def test_text_stays_the_default(repo):
    proc = _carto("build", "--repo", str(repo), cwd=repo)
    assert proc.returncode == 0
    assert proc.stdout.startswith("Full build: 1 files")


def test_anything_the_build_prints_goes_to_stderr(repo, capsys):
    """A library that prints during a build must not corrupt the envelope."""
    from unittest.mock import patch

    from cartograph import cli

    def noisy(**_kwargs):
        print("progress: 50%")
        return {"status": "ok", "build_type": "full", "files_parsed": 0,
                "total_nodes": 0, "total_edges": 0, "summary": "Full build complete"}

    argv = ["cartograph", "build", "--repo", str(repo), "--format", "json"]
    with patch.object(sys, "argv", argv), \
            patch("cartograph.tools.build.build_or_update_graph", noisy):
        with pytest.raises(SystemExit) as exc:
            cli.main()
    out, err = capsys.readouterr()
    assert exc.value.code == 0
    assert json.loads(out)["data"]["summary"] == "Full build complete"
    assert "progress: 50%" in err


def test_postprocess_json_is_one_envelope(repo):
    _carto("build", "--repo", str(repo), "--skip-postprocess", cwd=repo)
    proc = _carto("postprocess", "--repo", str(repo), "--format", "json", cwd=repo)
    assert proc.returncode == 0, proc.stderr
    doc = _envelope(proc)
    assert doc["tool"] == "postprocess" and doc["ok"] is True
    assert "fts_indexed" in doc["data"], doc["data"]


def test_postprocess_json_without_a_graph_is_a_precondition(repo):
    proc = _carto("postprocess", "--repo", str(repo), "--format", "json", cwd=repo)
    assert proc.returncode == 2, proc.stdout
    assert _envelope(proc)["error"]["remediation"] == "carto build"


def test_embed_json_with_an_unconfigured_provider_is_a_precondition(repo):
    """openai without its key fails in the provider's constructor, before any
    request is made, so this needs no network."""
    _carto("build", "--repo", str(repo), cwd=repo)
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENAI")}
    env["PYTHONPATH"] = str(ENGINE)
    proc = subprocess.run(
        [sys.executable, "-m", "cartograph", "embed", "--repo", str(repo),
         "--provider", "openai", "--format", "json"],
        cwd=repo, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
        timeout=120,
    )
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    doc = _envelope(proc)
    assert doc["error"]["code"] == "precondition"
    assert doc["error"]["remediation"]


def test_embed_json_without_a_graph_is_a_precondition(repo):
    proc = _carto("embed", "--repo", str(repo), "--format", "json", cwd=repo)
    assert proc.returncode == 2, proc.stdout
    assert _envelope(proc)["error"]["remediation"] == "carto build"
