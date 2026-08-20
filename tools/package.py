#!/usr/bin/env python3
"""Build the installable add-on from the single source tree in ``addon/``.

``addon/`` is the only place add-on code is edited.  ``neuroicu_tts_addon/`` is
the Anki ``addons21`` layout and is generated from it, so the two can never
drift apart by hand.  ``addon/test_packaging.py`` fails the suite when the
generated copy is stale.

Usage
-----
    python3 tools/package.py --check   # verify the generated copy is current
    python3 tools/package.py           # regenerate it
    python3 tools/package.py --zip     # regenerate and build the .ankiaddon
"""

from __future__ import annotations

import argparse
import filecmp
import hashlib
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "addon"
PACKAGE = ROOT / "neuroicu_tts_addon"
DIST = ROOT / "packages"
ARCHIVE = DIST / "neuroicu_tts_addon.ankiaddon"

# Tests, caches, and runtime artifacts stay out of the shipped add-on.
EXCLUDED_PREFIXES = ("test_",)
EXCLUDED_NAMES = {"neuroicu_tts.log"}
EXCLUDED_SUFFIXES = (".pyc", ".log", ".tmp")


def shipped_files() -> list[Path]:
    """Return the source files that belong in the installable add-on."""
    files = []
    for path in sorted(SOURCE.iterdir()):
        if not path.is_file():
            continue
        if path.name in EXCLUDED_NAMES:
            continue
        if path.name.startswith(EXCLUDED_PREFIXES) or path.suffix in EXCLUDED_SUFFIXES:
            continue
        files.append(path)
    return files


def stale_files() -> list[str]:
    """Return names that differ between the source tree and the built package."""
    expected = {path.name for path in shipped_files()}
    differences = []
    for path in shipped_files():
        target = PACKAGE / path.name
        if not target.exists() or not filecmp.cmp(path, target, shallow=False):
            differences.append(path.name)
    if PACKAGE.is_dir():
        for path in sorted(PACKAGE.iterdir()):
            if path.is_file() and path.name not in expected:
                differences.append(f"{path.name} (unexpected)")
    return sorted(differences)


def stale_archive() -> list[str]:
    """Return archive members that no longer match the source tree.

    The archive is an optional distribution artifact, so a missing one is not
    an error -- but a *present* one must be current.  Without this check the
    archive silently kept whatever was in it the last time --zip ran, which
    once shipped a build missing a worker fix that was already in the source.
    """
    if not ARCHIVE.exists():
        return []
    expected = {path.name: path for path in shipped_files()}
    differences = []
    with zipfile.ZipFile(ARCHIVE) as archive:
        packed = set(archive.namelist())
        for name in sorted(packed):
            source = expected.get(name)
            if source is None:
                differences.append(f"{name} (unexpected in archive)")
            elif hashlib.sha256(archive.read(name)).digest() != hashlib.sha256(source.read_bytes()).digest():
                differences.append(f"{name} (stale in archive)")
    differences.extend(f"{name} (missing from archive)" for name in sorted(set(expected) - packed))
    return differences


def build() -> list[str]:
    """Copy the shipped source files into the package directory."""
    PACKAGE.mkdir(parents=True, exist_ok=True)
    copied = []
    for path in shipped_files():
        shutil.copy2(path, PACKAGE / path.name)
        copied.append(path.name)
    for path in sorted(PACKAGE.iterdir()):
        if path.is_file() and path.name not in copied:
            path.unlink()
    return copied


def build_archive() -> Path:
    """Zip the package directory into an installable .ankiaddon archive."""
    DIST.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ARCHIVE, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(PACKAGE.iterdir()):
            if path.is_file() and path.suffix not in EXCLUDED_SUFFIXES:
                archive.write(path, path.name)
    return ARCHIVE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the package is out of date")
    parser.add_argument("--zip", action="store_true", help="also build the .ankiaddon archive")
    args = parser.parse_args(argv)

    if args.check:
        stale = stale_files()
        stale_zip = stale_archive()
        if stale or stale_zip:
            if stale:
                print("Package is out of date. Run: python3 tools/package.py", file=sys.stderr)
                for name in stale:
                    print(f"  - {name}", file=sys.stderr)
            if stale_zip:
                print("Archive is out of date. Run: python3 tools/package.py --zip", file=sys.stderr)
                for name in stale_zip:
                    print(f"  - {name}", file=sys.stderr)
            return 1
        print(f"Package is up to date ({len(shipped_files())} files).")
        return 0

    copied = build()
    print(f"Built {PACKAGE.relative_to(ROOT)} from {SOURCE.relative_to(ROOT)} ({len(copied)} files).")
    if args.zip:
        archive = build_archive()
        print(f"Wrote {archive.relative_to(ROOT)} ({archive.stat().st_size} bytes).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
