#!/usr/bin/env python3
"""Fail if any ELF file in a Linux payload needs a newer glibc than the floor.

    python3 scripts/check-glibc.py dist/payload/linux-x64            # floor 2.31
    python3 scripts/check-glibc.py dist/payload/linux-x64 --max 2.28

PyInstaller bundles libpython and most shared libraries but never libc, so
every ELF file in the payload runs against the target machine's glibc. The
newest ``GLIBC_x.y`` symbol version any one of them references is the oldest
distribution the payload can run on — and the failure beneath that floor is
not at start-up but whenever the offending library is first loaded. 0.8.0
shipped Debian 12's ``libstdc++.so.6`` (needs ``GLIBC_2.36``), so the graph
worked on Ubuntu 22.04 and the embedding runtime did not.

Reads the ELF version-needs section itself rather than shelling out to
``objdump -T``, so it runs the same on the build container and on a macOS
workstation checking a payload copied from CI.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

#: Debian 11 bullseye and Ubuntu 20.04, the oldest distributions the Linux
#: payload claims to support (docs/packaging.md, "Supported Linux").
DEFAULT_FLOOR = "2.31"

_SHT_GNU_VERNEED = 0x6FFFFFFE


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def glibc_needs(path: Path) -> dict[str, set[str]]:
    """``{"GLIBC_2.34": {"libc.so.6"}, …}`` for one file; empty if not ELF.

    Symbol versions are per needed library, so the library that asks for each
    version is kept: the report names both the payload file and the system
    library it expects the version from.
    """
    data = path.read_bytes()
    if data[:4] != b"\x7fELF" or len(data) < 64:
        return {}
    wide = data[4] == 2
    end = "<" if data[5] == 1 else ">"
    if wide:
        shoff, = struct.unpack_from(end + "Q", data, 0x28)
        shentsize, shnum = struct.unpack_from(end + "HH", data, 0x3A)
    else:
        shoff, = struct.unpack_from(end + "I", data, 0x20)
        shentsize, shnum = struct.unpack_from(end + "HH", data, 0x2E)

    def section(index: int) -> tuple[int, int, int, int]:
        """(type, offset, size, link) of section *index*."""
        base = shoff + index * shentsize
        if wide:
            _, kind, _, _, off, size, link = struct.unpack_from(end + "IIQQQQI", data, base)
        else:
            _, kind, _, _, off, size, link = struct.unpack_from(end + "IIIIIII", data, base)
        return kind, off, size, link

    needs: dict[str, set[str]] = {}
    for index in range(shnum):
        kind, off, _, link = section(index)
        if kind != _SHT_GNU_VERNEED:
            continue
        _, strtab, _, _ = section(link)

        def name(at: int) -> str:
            start = strtab + at
            return data[start:data.index(b"\0", start)].decode()

        entry = off
        while True:
            _, count, file_at, aux, following = struct.unpack_from(end + "HHIII", data, entry)
            library = name(file_at)
            at = entry + aux
            for _ in range(count):
                _, _, _, name_at, aux_next = struct.unpack_from(end + "IHHII", data, at)
                version = name(name_at)
                if version.startswith("GLIBC_") and version[6:7].isdigit():
                    needs.setdefault(version, set()).add(library)
                at += aux_next
            if not following:
                break
            entry += following
    return needs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("payload", type=Path)
    ap.add_argument("--max", default=DEFAULT_FLOOR, help=f"highest GLIBC allowed (default {DEFAULT_FLOOR})")
    args = ap.parse_args()
    floor = _version(args.max)

    per_file: list[tuple[tuple[int, ...], str, str]] = []
    for path in sorted(p for p in args.payload.rglob("*") if p.is_file() and not p.is_symlink()):
        needs = glibc_needs(path)
        if not needs:
            continue
        top = max(needs, key=lambda v: _version(v[6:]))
        per_file.append((_version(top[6:]), top,
                         f"{path.relative_to(args.payload)} ({', '.join(sorted(needs[top]))})"))

    if not per_file:
        raise SystemExit(f"FAIL: no ELF file under {args.payload}; wrong directory?")
    per_file.sort(reverse=True)
    over = [row for row in per_file if row[0] > floor]
    highest = per_file[0]
    print(f"glibc: {len(per_file)} ELF files; highest requirement {highest[1]} "
          f"({highest[2]}); floor GLIBC_{args.max}")
    # The top of the list is what a reader needs to see which library sets the
    # floor; the full list would be several hundred lines of grammars.
    for _, version, where in per_file[:10]:
        print(f"  {version:<12} {where}")
    if over:
        print(f"\nFAIL: {len(over)} file(s) need a glibc newer than {args.max}:")
        for _, version, where in over:
            print(f"  {version:<12} {where}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
