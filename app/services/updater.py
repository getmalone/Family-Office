"""
Self-update support for git-based installs.

The app is distributed as a git checkout; updates are released as ``vX.Y.Z`` tags.
This module checks whether a newer release exists and (from the launcher) applies
it: back up the database, check out the latest tag, and re-sync dependencies.

All git calls are best-effort and offline-safe — a missing network, missing git,
or a non-git (zip) install simply yields "no update available".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.version import get_app_version

_ROOT = Path(__file__).resolve().parent.parent.parent


def _git(*args: str, timeout: int = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(
            ["git", "-C", str(_ROOT), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, (p.stdout or "").strip()
    except Exception:
        return 1, ""


def is_git_install() -> bool:
    return (_ROOT / ".git").exists() and _git("rev-parse", "--git-dir")[0] == 0


def _parse(v: str) -> tuple:
    v = v.strip().lstrip("vV").split("+")[0].split("-")[0]
    parts = []
    for chunk in v.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


def latest_release_tag(fetch: bool = True) -> str | None:
    """Most recent vX.Y.Z tag from the origin (optionally fetching first)."""
    if not is_git_install():
        return None
    if fetch:
        _git("fetch", "--tags", "--quiet")
    code, out = _git("tag", "--list", "v*", "--sort=-v:refname")
    if code != 0 or not out:
        return None
    return out.splitlines()[0].strip()


def get_update_status(fetch: bool = True) -> dict:
    """Return current/latest version and whether an update is available."""
    current = get_app_version()
    status = {
        "current": current,
        "latest": None,
        "update_available": False,
        "is_git": is_git_install(),
    }
    if not status["is_git"]:
        return status
    latest = latest_release_tag(fetch=fetch)
    status["latest"] = latest
    if latest and _parse(latest) > _parse(current):
        status["update_available"] = True
    return status


def apply_update(tag: str) -> tuple[bool, str]:
    """Back up the DB, check out ``tag``, and re-sync dependencies.

    Returns (success, message). Intended to be called from the launcher, which
    then re-execs so the new code and dependencies take effect.
    """
    # Best-effort encrypted backup before changing anything.
    try:
        from app.services.app_settings import backup_database

        backup_database()
    except Exception:
        pass

    code, out = _git("checkout", "--quiet", tag)
    if code != 0:
        return False, f"git checkout {tag} failed: {out}"

    try:
        sync = subprocess.run(
            ["uv", "sync", "--frozen"], cwd=str(_ROOT),
            capture_output=True, text=True, timeout=600,
        )
        if sync.returncode != 0:
            return False, f"uv sync failed: {(sync.stderr or '').strip()[:300]}"
    except FileNotFoundError:
        return False, "uv not found on PATH"
    except Exception as exc:  # noqa: BLE001
        return False, f"uv sync error: {exc}"

    return True, f"updated to {tag}"
