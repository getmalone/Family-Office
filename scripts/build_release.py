#!/usr/bin/env python
"""
Assemble a clean, distributable bundle of the Family Office app.

Produces ``dist/family-office-<version>/`` and a matching ``.zip`` containing
everything a user needs to install on their laptop — and **nothing private**.
The real database, the `private/` import scripts, any `.env`, the local
virtualenv, and git history are all excluded, and the script hard-fails if any
secret-bearing path sneaks into the bundle.

Usage:
    ./.venv/bin/python scripts/build_release.py
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# What goes in the bundle.
INCLUDE_DIRS = ["app", "static", "launchers"]
INCLUDE_FILES = ["pyproject.toml", "uv.lock", "README.md", "USAGE.md"]

# Launchers + guide copied to the bundle root for an obvious double-click.
ROOT_LAUNCHERS = [
    "launchers/family-office-macos.command",
    "launchers/family-office-linux.sh",
    "launchers/family-office-windows.bat",
    "launchers/INSTALL.md",
]

# Never copy these (defense-in-depth; checked again after staging).
EXCLUDE_NAMES = {
    "__pycache__", ".git", ".venv", "venv", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules", ".DS_Store",
}
EXCLUDE_SUFFIXES = {".db", ".db-wal", ".db-shm", ".pyc"}
# Paths that must NOT appear anywhere in the staged bundle.
FORBIDDEN = ("private", ".env", "data")


def _version() -> str:
    text = (ROOT / "pyproject.toml").read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else "0.0.0"


def _ignore(_dir, names):
    return [
        n for n in names
        if n in EXCLUDE_NAMES or any(n.endswith(s) for s in EXCLUDE_SUFFIXES)
    ]


def _safety_check(stage: Path) -> None:
    problems = []
    for p in stage.rglob("*"):
        rel = p.relative_to(stage).as_posix()
        if p.name == ".env" or rel.startswith("private/") or rel.startswith("data/"):
            problems.append(rel)
        if p.suffix in EXCLUDE_SUFFIXES:
            problems.append(rel)
    if problems:
        print("ABORT — secret/private paths found in bundle:", file=sys.stderr)
        for p in sorted(set(problems)):
            print(f"  {p}", file=sys.stderr)
        raise SystemExit(2)


def main() -> None:
    version = _version()
    dist = ROOT / "dist"
    stage = dist / f"family-office-{version}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    for d in INCLUDE_DIRS:
        src = ROOT / d
        if src.exists():
            shutil.copytree(src, stage / d, ignore=_ignore)

    for f in INCLUDE_FILES:
        src = ROOT / f
        if src.exists():
            shutil.copy2(src, stage / f)

    # An .env.example (not a real .env) so users know what's configurable.
    if (ROOT / ".env.example").exists():
        shutil.copy2(ROOT / ".env.example", stage / ".env.example")

    # Surface the launchers + guide at the top level for an obvious double-click.
    for rel in ROOT_LAUNCHERS:
        src = ROOT / rel
        if src.exists():
            shutil.copy2(src, stage / Path(rel).name)

    # Keep the shell launchers executable.
    for name in ("family-office-macos.command", "family-office-linux.sh"):
        f = stage / name
        if f.exists():
            f.chmod(0o755)

    _safety_check(stage)

    archive = shutil.make_archive(str(dist / f"family-office-{version}"), "zip", stage)

    # Report.
    files = list(stage.rglob("*"))
    size_mb = sum(f.stat().st_size for f in files if f.is_file()) / 1e6
    print(f"✅ Built family-office {version}")
    print(f"   staged: {stage}  ({sum(f.is_file() for f in files)} files, {size_mb:.1f} MB)")
    print(f"   zip:    {archive}  ({Path(archive).stat().st_size / 1e6:.1f} MB)")
    print("   top level:")
    for p in sorted(stage.iterdir()):
        print(f"     {'📁' if p.is_dir() else '📄'} {p.name}")


if __name__ == "__main__":
    main()
