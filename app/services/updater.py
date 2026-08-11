"""
Self-update support — git checkouts AND zip (manual) installs.

Git installs: updates are ``vX.Y.Z`` tags; apply = checkout tag + re-sync deps.

Zip installs (how the app is actually distributed to laptops): updates are
GitHub Releases carrying a ``family-office-<version>.zip`` bundle. Apply =
download the bundle, verify it, and swap the CODE in place — only the
allowlisted app directories/files are replaced, so ``data/`` (the encrypted
database), ``.env``, ``.venv``, and ``backups/`` are never touched. The next
launch's ``uv sync --frozen`` installs the new dependency set, exactly like a
first install.

All network/git calls are best-effort and offline-safe — failure yields
"no update available" or a clear error message, never a broken install.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from app.version import get_app_version

_ROOT = Path(__file__).resolve().parent.parent.parent

GITHUB_REPO = "getmalone/Family-Office"
_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

# What a release bundle is allowed to replace in the install directory. This is
# the flip side of scripts/build_release.py's include list — everything else in
# the install dir (data/, .env, .venv, backups/) belongs to the user.
_SWAP_DIRS = ["app", "static", "launchers", "alembic", "scripts"]
_SWAP_FILES = [
    "pyproject.toml", "uv.lock", "README.md", "USAGE.md", "alembic.ini",
    # Double-click launchers duplicated at the bundle root for discoverability.
    "family-office-macos.command", "family-office-linux.sh",
    "family-office-windows.bat", "INSTALL.md",
]


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


def latest_manual_release(timeout: float = 15.0) -> dict | None:
    """Latest GitHub release + its bundle asset, for zip installs.

    Returns {"tag", "zip_url", "size"} or None (offline, rate-limited, or no
    release with a bundle attached). Unauthenticated — the repo is public.
    """
    try:
        import httpx

        resp = httpx.get(
            _API_LATEST, timeout=timeout, follow_redirects=True,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "family-office-updater"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None
    tag = (data.get("tag_name") or "").strip()
    if not tag:
        return None
    for asset in data.get("assets") or []:
        name = asset.get("name") or ""
        if re.fullmatch(r"family-office-.*\.zip", name):
            return {
                "tag": tag,
                "zip_url": asset.get("browser_download_url"),
                "size": int(asset.get("size") or 0),
            }
    return None


def get_update_status(fetch: bool = True) -> dict:
    """Return current/latest version and whether an update is available.

    Works for both install modes: git checkouts ask the origin's tags; zip
    installs ask GitHub Releases. ``fetch=False`` skips the network entirely
    (latest stays unknown for zip installs).
    """
    current = get_app_version()
    status = {
        "current": current,
        "latest": None,
        "update_available": False,
        "is_git": is_git_install(),
    }
    status["mode"] = "git" if status["is_git"] else "manual"
    if status["is_git"]:
        latest = latest_release_tag(fetch=fetch)
    elif fetch:
        rel = latest_manual_release()
        latest = rel["tag"] if rel else None
    else:
        latest = None
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


# ── Zip (manual) installs ─────────────────────────────────────────────────────


def _download_zip(url: str, dest: Path, timeout: float = 120.0) -> None:
    import httpx

    with httpx.stream(
        "GET", url, timeout=timeout, follow_redirects=True,
        headers={"User-Agent": "family-office-updater"},
    ) as resp:
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_bytes():
                f.write(chunk)


def _safe_extract(zip_path: Path, target: Path) -> None:
    """Extract with a zip-slip guard: every member must land under target."""
    with zipfile.ZipFile(zip_path) as zf:
        target_resolved = target.resolve()
        for member in zf.namelist():
            dest = (target / member).resolve()
            if not dest.is_relative_to(target_resolved):
                raise ValueError(f"unsafe path in bundle: {member}")
        zf.extractall(target)


def _bundle_root(extract_dir: Path) -> Path | None:
    """The family-office-<version>/ directory inside an extracted bundle."""
    for child in sorted(extract_dir.iterdir()):
        if child.is_dir() and (child / "pyproject.toml").exists() and (child / "app").is_dir():
            return child
    return None


def _pyproject_version(pyproject: Path) -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(), re.MULTILINE)
    return m.group(1) if m else "0.0.0"


def apply_manual_update() -> tuple[bool, str]:
    """Download the latest release bundle and swap the app code in place.

    Replaces only the allowlisted code paths — the encrypted database
    (``data/``), ``.env``, ``.venv``, and ``backups/`` are never touched. The
    caller tells the user to relaunch; the launcher's ``uv sync --frozen`` then
    installs the new dependency set from the swapped lockfile.
    """
    rel = latest_manual_release()
    if rel is None:
        return False, "could not reach GitHub Releases (offline or rate-limited) — try again shortly"
    current = get_app_version()
    if _parse(rel["tag"]) <= _parse(current):
        return False, f"already up to date (v{current})"
    if not rel.get("zip_url"):
        return False, f"release {rel['tag']} has no downloadable bundle"

    # Best-effort encrypted backup before changing anything.
    try:
        from app.services.app_settings import backup_database

        backup_database()
    except Exception:
        pass

    try:
        with tempfile.TemporaryDirectory(prefix="kfo-update-") as tmp:
            tmp_path = Path(tmp)
            zip_path = tmp_path / "bundle.zip"
            _download_zip(rel["zip_url"], zip_path)

            extract_dir = tmp_path / "extracted"
            extract_dir.mkdir()
            _safe_extract(zip_path, extract_dir)

            bundle = _bundle_root(extract_dir)
            if bundle is None:
                return False, "downloaded bundle is malformed (no app/ + pyproject.toml)"
            bundle_version = _pyproject_version(bundle / "pyproject.toml")
            if _parse(f"v{bundle_version}") != _parse(rel["tag"]):
                return False, (
                    f"bundle version {bundle_version} doesn't match release {rel['tag']}"
                )

            # Swap code into place. Only after the whole bundle downloaded,
            # extracted, and verified — a network failure can't half-apply.
            for name in _SWAP_DIRS:
                src = bundle / name
                if not src.is_dir():
                    continue
                dst = _ROOT / name
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
            for name in _SWAP_FILES:
                src = bundle / name
                if src.is_file():
                    shutil.copy2(src, _ROOT / name)

        # Zip archives don't reliably carry the executable bit — restore it on
        # the double-click launchers so the relaunch actually double-clicks.
        for pattern in ("*.command", "*.sh"):
            for p in list(_ROOT.glob(pattern)) + list((_ROOT / "launchers").glob(pattern)):
                try:
                    p.chmod(p.stat().st_mode | 0o755)
                except Exception:
                    pass
    except Exception as exc:  # noqa: BLE001
        return False, f"update failed: {exc}"

    return True, (
        f"updated to {rel['tag']} — quit this app (close its terminal window) and "
        "double-click Family Office to relaunch; the update finishes automatically"
    )
