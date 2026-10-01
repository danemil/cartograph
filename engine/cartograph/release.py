"""Which Cartograph release this engine belongs to.

A release is bumped in one place, ``extension/package.json`` — the version the
``.vsix`` carries and the one people quote. The engine package keeps the
version of the upstream code-review-graph release it forked from
(``cartograph.__version__``, also in ``pyproject.toml``), which says where the
code came from, not which build someone is running.

A frozen build has no checkout beside it, so ``scripts/build-payload.py``
writes the release into a stamp file bundled next to this module. That file
exists only in a payload; a source checkout reads ``package.json`` directly.

This module has no package-relative imports at load time: the payload build
loads it by path, without importing the engine, so both read the release the
same way.
"""

from __future__ import annotations

import json
from pathlib import Path

STAMP_NAME = "RELEASE"
_HERE = Path(__file__).resolve().parent
_STAMP = _HERE / STAMP_NAME
# engine/cartograph/release.py -> <repo>/extension/package.json
_PACKAGE_JSON = _HERE.parents[1] / "extension" / "package.json"

UPSTREAM_VERSION = "2.3.8"
UPSTREAM_NAME = "code-review-graph"


def read_package_version(package_json: Path) -> str | None:
    try:
        version = json.loads(package_json.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        return None
    return version if isinstance(version, str) and version else None


def release_version(stamp: Path = _STAMP, package_json: Path = _PACKAGE_JSON) -> str:
    """The release, or ``unreleased`` for an engine outside both a payload
    and a checkout (a bare ``pip install``), which no release describes."""
    try:
        stamped = stamp.read_text(encoding="utf-8").strip()
    except OSError:
        stamped = ""
    return stamped or read_package_version(package_json) or "unreleased"


def version_line() -> str:
    return (
        f"cartograph {release_version()} "
        f"(engine fork of {UPSTREAM_NAME} {UPSTREAM_VERSION})"
    )
