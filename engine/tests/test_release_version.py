"""`carto --version` names the Cartograph release, and says what it forked.

The release is whatever `extension/package.json` says: that is the file a
release bump edits, and the version the `.vsix` carries. A frozen build has no
checkout beside it, so the payload build stamps the same value into the bundle.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from cartograph import cli, release

ENGINE = Path(__file__).resolve().parents[1]
REPO = ENGINE.parent
PACKAGE_JSON = REPO / "extension" / "package.json"


def _shipped() -> str:
    return json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))["version"]


def test_source_checkout_reports_the_extension_version():
    assert release.release_version() == _shipped()


def test_stamp_wins_over_the_checkout(tmp_path):
    stamp = tmp_path / release.STAMP_NAME
    stamp.write_text("9.9.9\n", encoding="utf-8")
    assert release.release_version(stamp=stamp, package_json=PACKAGE_JSON) == "9.9.9"


def test_neither_source_says_unreleased(tmp_path):
    assert release.release_version(
        stamp=tmp_path / "absent", package_json=tmp_path / "absent.json"
    ) == "unreleased"


def test_unreadable_package_json_says_unreleased(tmp_path):
    bad = tmp_path / "package.json"
    bad.write_text("{not json", encoding="utf-8")
    assert release.release_version(stamp=tmp_path / "absent", package_json=bad) == "unreleased"


def test_version_line_names_release_and_upstream():
    line = release.version_line()
    assert line == (
        f"cartograph {_shipped()} (engine fork of code-review-graph {release.UPSTREAM_VERSION})"
    )


def test_upstream_version_is_the_package_attribute():
    import cartograph

    assert release.UPSTREAM_VERSION == cartograph.__version__ == "2.3.8"


def test_cli_version_flag_prints_the_release():
    out = subprocess.run(
        [sys.executable, "-m", "cartograph", "--version"],
        cwd=ENGINE, capture_output=True, text=True, timeout=120, check=True,
    ).stdout.strip()
    assert out == release.version_line()
    assert out.split()[1] == _shipped()


def test_capabilities_carries_the_release(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["carto", "capabilities", "--format", "json"])
    with pytest.raises(SystemExit):
        cli.main()
    env = json.loads(capsys.readouterr().out)
    assert env["data"]["version"] == _shipped()


def _build_payload_module():
    spec = importlib.util.spec_from_file_location(
        "build_payload", REPO / "scripts" / "build-payload.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_payload_build_stamps_the_release(tmp_path):
    bp = _build_payload_module()
    assert bp.engine_version() == _shipped()
    stamp = bp.write_release_stamp(tmp_path)
    assert stamp.name == release.STAMP_NAME
    assert stamp.read_text(encoding="utf-8").strip() == _shipped()
    # What the frozen engine will read back from that file.
    assert release.release_version(stamp=stamp, package_json=tmp_path / "x") == _shipped()
