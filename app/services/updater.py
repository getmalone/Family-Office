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


# Some networks (corporate laptops especially) allow github.com but block
# api.github.com, or intercept TLS on it. The web endpoint redirects to the
# newest release's tag, which is enough to rebuild the bundle URL ourselves —
# our asset naming is deterministic.
_WEB_LATEST = f"https://github.com/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"


def _bundle_url(tag: str) -> str:
    return (f"https://github.com/{GITHUB_REPO}/releases/download/{tag}/"
            f"family-office-{tag.lstrip('v')}.zip")


def _describe(exc: Exception) -> str:
    """A one-line cause the user can act on, not just 'it failed'."""
    name = type(exc).__name__
    text = str(exc).strip()
    if "certificate" in text.lower() or "SSL" in name or "ssl" in text.lower():
        return ("TLS verification failed — a network that inspects HTTPS "
                "(corporate proxy or VPN) will block this")
    if "proxy" in text.lower() or "Proxy" in name:
        return f"proxy error: {text[:120]}"
    if "ConnectTimeout" in name or "ReadTimeout" in name or "Timeout" in name:
        return "the connection timed out"
    if "ConnectError" in name or "NameResolution" in text or "getaddrinfo" in text:
        return "could not connect (offline, DNS, or a firewall blocking github.com)"
    return f"{name}: {text[:120]}" if text else name


def _release_from_api(timeout: float) -> tuple[dict | None, str | None]:
    import httpx

    try:
        resp = httpx.get(
            _API_LATEST, timeout=timeout, follow_redirects=True,
            headers={"Accept": "application/vnd.github+json",
                     "User-Agent": "family-office-updater"},
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"api.github.com — {_describe(exc)}"
    if resp.status_code == 403 and "rate limit" in (getattr(resp, "text", "") or "").lower():
        return None, ("GitHub rate-limited this network (60 checks an hour are "
                      "shared by everyone on your IP) — try again in an hour")
    if resp.status_code != 200:
        return None, f"api.github.com returned HTTP {resp.status_code}"
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        return None, "api.github.com returned a response that was not JSON"
    tag = (data.get("tag_name") or "").strip()
    if not tag:
        return None, "the latest release has no tag"
    for asset in data.get("assets") or []:
        if re.fullmatch(r"family-office-.*\.zip", asset.get("name") or ""):
            return {
                "tag": tag,
                "zip_url": asset.get("browser_download_url"),
                "size": int(asset.get("size") or 0),
            }, None
    return None, f"release {tag} has no family-office zip attached"


def _release_from_web(timeout: float) -> tuple[dict | None, str | None]:
    """Fallback: read the tag from github.com's /releases/latest redirect."""
    import httpx

    try:
        resp = httpx.get(
            _WEB_LATEST, timeout=timeout, follow_redirects=False,
            headers={"User-Agent": "family-office-updater"},
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"github.com — {_describe(exc)}"
    location = (getattr(resp, "headers", None) or {}).get("location", "")
    if resp.status_code not in (301, 302, 303, 307, 308) or "/tag/" not in location:
        return None, f"github.com returned HTTP {resp.status_code}"
    tag = location.rsplit("/tag/", 1)[-1].strip("/")
    if not tag:
        return None, "github.com did not name a release tag"
    return {"tag": tag, "zip_url": _bundle_url(tag), "size": 0}, None


def latest_release_detailed(timeout: float = 15.0) -> tuple[dict | None, str | None]:
    """Latest release bundle, plus why the lookup failed when it did.

    Tries the JSON API first and falls back to the github.com web redirect, so a
    network that blocks only api.github.com still updates. Returns
    ({"tag", "zip_url", "size"}, None) or (None, reason).
    """
    rel, api_error = _release_from_api(timeout)
    if rel:
        return rel, None
    rel, web_error = _release_from_web(timeout)
    if rel:
        return rel, None
    return None, api_error or web_error


def latest_manual_release(timeout: float = 15.0) -> dict | None:
    """Latest GitHub release + its bundle asset, for zip installs.

    Returns {"tag", "zip_url", "size"} or None (offline, blocked, rate-limited,
    or no release with a bundle attached). Unauthenticated — the repo is public.
    """
    rel, _ = latest_release_detailed(timeout)
    return rel


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
        rel, error = latest_release_detailed()
        latest = rel["tag"] if rel else None
        if error:
            status["error"] = error
            status["releases_url"] = RELEASES_PAGE
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
    """Locate the app inside an extracted bundle.

    Real bundles are FLAT — build_release.py zips the staging dir's *contents*
    (shutil.make_archive with root_dir=stage), so pyproject.toml/app/ sit at
    the archive top level. Also accepts a single family-office-<version>/
    wrapper dir, in case the packaging ever changes shape.
    """
    if (extract_dir / "pyproject.toml").exists() and (extract_dir / "app").is_dir():
        return extract_dir
    for child in sorted(extract_dir.iterdir()):
        if child.is_dir() and (child / "pyproject.toml").exists() and (child / "app").is_dir():
            return child
    return None


def _pyproject_version(pyproject: Path) -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(), re.MULTILINE)
    return m.group(1) if m else "0.0.0"


def _install_bundle(zip_path: Path, expected_tag: str | None = None) -> tuple[bool, str]:
    """Verify a release zip and swap its code into the install directory.

    Only the allowlisted code paths are replaced — the encrypted database
    (``data/``), ``.env``, ``.venv``, and ``backups/`` are never touched. The
    swap happens only after the bundle is fully extracted and version-checked,
    so a truncated or wrong-version file can never half-apply.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="kfo-update-") as tmp:
            extract_dir = Path(tmp) / "extracted"
            extract_dir.mkdir()
            _safe_extract(zip_path, extract_dir)

            bundle = _bundle_root(extract_dir)
            if bundle is None:
                return False, "that file isn't a Family Office bundle (no app/ + pyproject.toml)"
            bundle_version = _pyproject_version(bundle / "pyproject.toml")
            if expected_tag and _parse(f"v{bundle_version}") != _parse(expected_tag):
                return False, (
                    f"bundle version {bundle_version} doesn't match release {expected_tag}"
                )
            if _parse(f"v{bundle_version}") <= _parse(get_app_version()):
                return False, (f"that bundle is v{bundle_version}, which is not newer "
                               f"than the running v{get_app_version()}")

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
            for f in list(_ROOT.glob(pattern)) + list((_ROOT / "launchers").glob(pattern)):
                try:
                    f.chmod(f.stat().st_mode | 0o755)
                except Exception:
                    pass
    except Exception as exc:  # noqa: BLE001
        return False, f"update failed: {exc}"

    return True, (
        f"updated to v{bundle_version} — quit this app (close its terminal window) and "
        "double-click Family Office to relaunch; the update finishes automatically"
    )


def apply_bundle_file(data: bytes) -> tuple[bool, str]:
    """Install an update from a release zip the user downloaded themselves.

    The escape hatch for a network the app cannot reach directly: the browser
    fetches the bundle through whatever proxy it is configured with, and the app
    installs the bytes it is handed. Same verification and same swap as an
    automatic update — nothing about the file is trusted beyond its contents.
    """
    if not data:
        return False, "that file was empty"

    try:
        from app.services.app_settings import backup_database

        backup_database()
    except Exception:
        pass

    with tempfile.TemporaryDirectory(prefix="kfo-upload-") as tmp:
        zip_path = Path(tmp) / "bundle.zip"
        zip_path.write_bytes(data)
        return _install_bundle(zip_path)


def apply_manual_update() -> tuple[bool, str]:
    """Download the latest release bundle and swap the app code in place.

    Replaces only the allowlisted code paths — the encrypted database
    (``data/``), ``.env``, ``.venv``, and ``backups/`` are never touched. The
    caller tells the user to relaunch; the launcher's ``uv sync --frozen`` then
    installs the new dependency set from the swapped lockfile.
    """
    rel, error = latest_release_detailed()
    if rel is None:
        return False, error or "could not reach GitHub Releases — try again shortly"
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
            zip_path = Path(tmp) / "bundle.zip"
            _download_zip(rel["zip_url"], zip_path)
            return _install_bundle(zip_path, expected_tag=rel["tag"])
    except Exception as exc:  # noqa: BLE001
        return False, f"download failed: {exc}"
