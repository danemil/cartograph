"""The repository's layout: every file, not only the ones the graph parsed.

The code graph sees parsed source. On a repository that is mostly
documentation and templates, an overview built from the graph alone describes
a small corner of it as if it were the whole — a measured Copilot run answered
"how is this repository structured" wrongly five times out of five from
``carto architecture`` alone, where reading the README got it right. So the
overview also says what the repository is made of, by top-level directory and
kind of file, and says so plainly when most of it is not code.

Kinds are decided by file name alone; reading files to classify them would
make the overview cost what a build costs.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional

from .incremental import _load_ignore_patterns, _should_ignore, repository_files
from .parser import EXTENSION_TO_LANGUAGE

KINDS = ("code", "docs", "config", "other")

_DOCS = frozenset({
    ".md", ".mdx", ".markdown", ".rst", ".txt", ".adoc", ".asciidoc", ".org",
    ".tex",
})
_DOCS_NAMES = frozenset({"readme", "license", "licence", "notice", "changelog",
                         "authors", "contributing", "copying"})
_CONFIG = frozenset({
    ".json", ".jsonc", ".json5", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".conf", ".env", ".properties", ".xml", ".lock", ".plist", ".editorconfig",
})
_CONFIG_NAMES = frozenset({"dockerfile", "makefile", "procfile", "gemfile",
                           "rakefile", "justfile", "codeowners", "cmakelists",
                           "constraints"})
#: Source the graph does not parse is still code.
_CODE_EXTRA = frozenset({".html", ".htm", ".css", ".scss", ".sass", ".less",
                         ".mm", ".fs", ".hs", ".ml", ".clj", ".erl", ".bat",
                         ".cmd", ".groovy", ".gradle"})
#: YAML and properties are parsed, but they are configuration to a reader.
_CODE = frozenset(
    ext for ext, lang in EXTENSION_TO_LANGUAGE.items()
    if lang not in ("yaml", "properties")
) | _CODE_EXTRA

#: Top-level directories named before the rest are summed into one line.
MAX_DIRS = 12
#: Root files named as the place to read first, in this order of preference.
_NOTABLE = ("readme", "agents", "claude", "architecture", "contributing",
            "copilot-instructions")
MAX_NOTABLE = 4
#: Below this share of code files the overview says the graph is not the
#: whole picture.
CODE_MAJORITY = 50


def kind_of(path: str) -> str:
    name = PurePosixPath(path).name.lower()
    stem, dot, ext = name.lstrip(".").rpartition(".")
    stem, suffix = (stem, f".{ext}") if dot else (ext, "")
    # Named by convention, whatever their extension says.
    if stem in _CONFIG_NAMES or stem.startswith("requirements"):
        return "config"
    if suffix in _DOCS or stem in _DOCS_NAMES:
        return "docs"
    if suffix in _CONFIG or name.startswith("."):
        return "config"
    if suffix in _CODE:
        return "code"
    return "other"


def _notable(root_files: Iterable[str]) -> list[str]:
    ranked = []
    for name in root_files:
        lowered = name.lower()
        for rank, prefix in enumerate(_NOTABLE):
            if lowered.startswith(prefix) and kind_of(name) == "docs":
                ranked.append((rank, name))
                break
    return [name for _, name in sorted(ranked)][:MAX_NOTABLE]


def _pct(part: int, whole: int) -> int:
    return round(100 * part / whole) if whole else 0


def summarise(files: list[str], graph_files: set[str]) -> dict[str, Any]:
    """Counts by top-level directory and kind, and the graph's share.

    ``files`` and ``graph_files`` are repo-relative POSIX paths.
    """
    by_kind: Counter[str] = Counter()
    dirs: dict[str, Counter[str]] = {}
    root_files: list[str] = []
    for path in files:
        kind = kind_of(path)
        by_kind[kind] += 1
        head, sep, _ = path.partition("/")
        if sep:
            dirs.setdefault(f"{head}/", Counter())[kind] += 1
        else:
            root_files.append(path)
            dirs.setdefault(".", Counter())[kind] += 1
    total = len(files)
    in_graph = sum(1 for path in files if path in graph_files)

    root = dirs.pop(".", None)
    ranked = sorted(dirs.items(), key=lambda item: (-sum(item[1].values()), item[0]))
    rows: list[dict[str, Any]] = []
    notable = _notable(root_files)
    if root:
        rows.append({"dir": "(root)", "files": sum(root.values()),
                     "kinds": _ordered(root), "notable": notable})
    rows.extend({"dir": d, "files": sum(c.values()), "kinds": _ordered(c)}
                for d, c in ranked[:MAX_DIRS])
    layout: dict[str, Any] = {
        "files": total,
        "by_kind": _ordered(by_kind),
        "graph_files": in_graph,
        "dirs": rows,
    }
    rest = ranked[MAX_DIRS:]
    if rest:
        layout["dirs_omitted"] = {
            "dirs": len(rest), "files": sum(sum(c.values()) for _, c in rest),
        }
    non_code = total - by_kind["code"]
    if total and _pct(by_kind["code"], total) < CODE_MAJORITY:
        # `by_kind` carries the breakdown; the note says only what it means.
        note = (f"{_pct(non_code, total)}% of tracked files are not code; "
                f"the code graph covers {_pct(in_graph, total)}%. "
                "Communities below describe only that part; read ")
        layout["note"] = note + (", ".join(notable) if notable
                                 else "the top-level docs") + " for the rest."
    return layout


def _ordered(counts: Counter[str]) -> dict[str, int]:
    return {k: counts[k] for k in KINDS if counts[k]}


def repository_layout(root: Path, graph_paths: Iterable[str]) -> dict[str, Any]:
    """The layout of the repository at ``root``.

    Tracked files are counted whole: a tracked ``dist/`` is still part of the
    repository a reader is asking about. Only a walk without version control
    applies the ignore patterns, which is what keeps ``.git`` out of it.
    """
    files, tracked = repository_files(root)
    files = [f.replace("\\", "/") for f in files]
    if not tracked:
        ignore = _load_ignore_patterns(root)
        files = [f for f in files if not _should_ignore(f, ignore)]
    graph_files: set[str] = set()
    for path in graph_paths:
        rel = _relative(path, root)
        if rel:
            graph_files.add(rel)
    return summarise(files, graph_files)


def _relative(path: str, root: Path) -> Optional[str]:
    p = Path(path)
    if not p.is_absolute():
        return PurePosixPath(path.replace("\\", "/")).as_posix()
    try:
        return p.relative_to(root).as_posix()
    except ValueError:
        try:
            return p.resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            return None
