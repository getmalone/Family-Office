"""Zip-install self-update: GitHub release discovery, bundle verify, in-place
code swap that never touches user data (data/, .env, .venv, backups/)."""

import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import updater


# ── Release discovery ────────────────────────────────────────────────────────

def _fake_httpx_get(payload, status=200):
    def _get(url, **kw):
        return SimpleNamespace(status_code=status, json=lambda: payload)
    return _get


def test_latest_manual_release_parses_release_json(monkeypatch):
    import httpx
    payload = {
        "tag_name": "v0.1.19",
        "assets": [
            {"name": "notes.txt", "browser_download_url": "http://x/n", "size": 1},
            {"name": "family-office-0.1.19.zip",
             "browser_download_url": "http://x/family-office-0.1.19.zip", "size": 700000},
        ],
    }
    monkeypatch.setattr(httpx, "get", _fake_httpx_get(payload))
    rel = updater.latest_manual_release()
    assert rel == {"tag": "v0.1.19",
                   "zip_url": "http://x/family-office-0.1.19.zip", "size": 700000}


def test_latest_manual_release_handles_misses(monkeypatch):
    import httpx
    monkeypatch.setattr(httpx, "get", _fake_httpx_get({}, status=403))
    assert updater.latest_manual_release() is None          # rate-limited
    monkeypatch.setattr(httpx, "get", _fake_httpx_get({"tag_name": "v1", "assets": []}))
    assert updater.latest_manual_release() is None          # no bundle asset


def test_status_manual_mode(monkeypatch):
    monkeypatch.setattr(updater, "is_git_install", lambda: False)
    monkeypatch.setattr(updater, "get_app_version", lambda: "0.1.18")
    monkeypatch.setattr(updater, "latest_release_detailed",
                        lambda **kw: ({"tag": "v0.1.19", "zip_url": "u", "size": 1}, None))
    s = updater.get_update_status(fetch=True)
    assert s["mode"] == "manual" and s["latest"] == "v0.1.19" and s["update_available"]

    # fetch=False must not touch the network at all.
    monkeypatch.setattr(updater, "latest_release_detailed",
                        lambda **kw: (_ for _ in ()).throw(AssertionError("network hit")))
    s2 = updater.get_update_status(fetch=False)
    assert s2["latest"] is None and not s2["update_available"]


# ── Bundle apply ─────────────────────────────────────────────────────────────

def _make_install(root: Path) -> None:
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("OLD")
    (root / "static").mkdir()
    (root / "static" / "old-only.js").write_text("stale")
    (root / "data").mkdir()
    (root / "data" / "family_office.db").write_bytes(b"PRECIOUS")
    (root / ".env").write_text("KFO_SECRET=1")
    (root / ".venv").mkdir()
    (root / ".venv" / "marker").write_text("venv")
    (root / "pyproject.toml").write_text('version = "0.1.18"\n')


def _make_bundle_zip(path: Path, version="9.9.9", wrapped=False) -> Path:
    """Craft a release bundle zip. Real bundles are FLAT (contents at the
    archive top level — that's what shutil.make_archive(root_dir=stage)
    produces); wrapped=True builds the tolerated wrapper-dir variant."""
    bundle = path / f"family-office-{version}"
    (bundle / "app").mkdir(parents=True)
    (bundle / "app" / "__init__.py").write_text("NEW")
    (bundle / "app" / "brand_new.py").write_text("NEW FILE")
    (bundle / "static").mkdir()
    (bundle / "static" / "new.js").write_text("fresh")
    (bundle / "pyproject.toml").write_text(f'version = "{version}"\n')
    (bundle / "uv.lock").write_text("lock")
    (bundle / "family-office-macos.command").write_text("#!/bin/bash\n")
    zip_path = path / "bundle.zip"
    arc_base = path if wrapped else bundle
    with zipfile.ZipFile(zip_path, "w") as zf:
        for p in bundle.rglob("*"):
            zf.write(p, p.relative_to(arc_base))
    return zip_path


@pytest.fixture
def fake_install(tmp_path, monkeypatch):
    root = tmp_path / "install"
    _make_install(root)
    zip_path = _make_bundle_zip(tmp_path / "bundlesrc")
    monkeypatch.setattr(updater, "_ROOT", root)
    monkeypatch.setattr(updater, "get_app_version", lambda: "0.1.18")
    monkeypatch.setattr(updater, "latest_release_detailed",
                        lambda **kw: ({"tag": "v9.9.9", "zip_url": "http://x/b.zip", "size": 1}, None))
    monkeypatch.setattr(updater, "_download_zip",
                        lambda url, dest, **kw: dest.write_bytes(zip_path.read_bytes()))
    return root


def test_apply_accepts_wrapped_bundle_shape(fake_install, tmp_path, monkeypatch):
    wrapped = _make_bundle_zip(tmp_path / "wrapped", wrapped=True)
    monkeypatch.setattr(updater, "_download_zip",
                        lambda url, dest, **kw: dest.write_bytes(wrapped.read_bytes()))
    ok, msg = updater.apply_manual_update()
    assert ok, msg
    assert (fake_install / "app" / "__init__.py").read_text() == "NEW"


def test_apply_swaps_code_and_preserves_user_data(fake_install):
    ok, msg = updater.apply_manual_update()
    assert ok, msg
    assert "v9.9.9" in msg and "relaunch" in msg

    root = fake_install
    assert (root / "app" / "__init__.py").read_text() == "NEW"
    assert (root / "app" / "brand_new.py").exists()
    assert (root / "static" / "new.js").exists()
    assert not (root / "static" / "old-only.js").exists()   # dir fully replaced
    assert 'version = "9.9.9"' in (root / "pyproject.toml").read_text()

    # The user's world is untouched.
    assert (root / "data" / "family_office.db").read_bytes() == b"PRECIOUS"
    assert (root / ".env").read_text() == "KFO_SECRET=1"
    assert (root / ".venv" / "marker").exists()

    # Launcher regains its double-click bit.
    assert (root / "family-office-macos.command").stat().st_mode & 0o111


def test_apply_refuses_when_already_current(fake_install, monkeypatch):
    monkeypatch.setattr(updater, "latest_release_detailed",
                        lambda **kw: ({"tag": "v0.1.18", "zip_url": "u", "size": 1}, None))
    ok, msg = updater.apply_manual_update()
    assert not ok and "already up to date" in msg
    assert (fake_install / "app" / "__init__.py").read_text() == "OLD"


def test_apply_rejects_version_mismatch_bundle(fake_install, tmp_path, monkeypatch):
    lying_zip = _make_bundle_zip(tmp_path / "liar", version="8.8.8")
    monkeypatch.setattr(updater, "_download_zip",
                        lambda url, dest, **kw: dest.write_bytes(lying_zip.read_bytes()))
    ok, msg = updater.apply_manual_update()   # release says v9.9.9, bundle says 8.8.8
    assert not ok and "doesn't match" in msg
    assert (fake_install / "app" / "__init__.py").read_text() == "OLD"   # nothing swapped


def test_safe_extract_blocks_zip_slip(tmp_path):
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../outside.txt", "nope")
    with pytest.raises(ValueError, match="unsafe path"):
        updater._safe_extract(evil, tmp_path / "out")


# ── Endpoints ────────────────────────────────────────────────────────────────

def test_check_updates_is_no_store(test_client, monkeypatch):
    monkeypatch.setattr(updater, "get_update_status",
                        lambda fetch=True: {"current": "0.1.18", "latest": None,
                                            "update_available": False, "is_git": False,
                                            "mode": "manual"})
    r = test_client.get("/settings/check-updates")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert r.json()["mode"] == "manual"


def test_apply_update_endpoint_routes_manual(test_client, monkeypatch):
    monkeypatch.setattr(updater, "get_update_status",
                        lambda fetch=True: {"current": "0.1.18", "latest": "v9.9.9",
                                            "update_available": True, "is_git": False,
                                            "mode": "manual"})
    monkeypatch.setattr(updater, "apply_manual_update", lambda: (True, "updated to v9.9.9"))
    r = test_client.post("/settings/apply-update")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "message": "updated to v9.9.9", "restart_required": True}


def test_apply_update_endpoint_409_when_current(test_client, monkeypatch):
    monkeypatch.setattr(updater, "get_update_status",
                        lambda fetch=True: {"current": "0.1.18", "latest": "v0.1.18",
                                            "update_available": False, "is_git": False,
                                            "mode": "manual"})
    assert test_client.post("/settings/apply-update").status_code == 409


# ── Diagnostics + github.com fallback ────────────────────────────────────────

def _responder(by_url):
    """Fake httpx.get that answers differently per host, or raises."""
    def _get(url, **kw):
        for fragment, outcome in by_url.items():
            if fragment in url:
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        raise AssertionError(f"unexpected url {url}")
    return _get


def test_web_fallback_when_the_api_host_is_blocked(monkeypatch):
    """Networks that allow github.com but block api.github.com still update."""
    import httpx

    monkeypatch.setattr(httpx, "get", _responder({
        "api.github.com": httpx.ConnectError("connection refused"),
        "github.com/getmalone": SimpleNamespace(
            status_code=302,
            headers={"location": "https://github.com/getmalone/Family-Office"
                                 "/releases/tag/v0.1.24"},
        ),
    }))
    rel, error = updater.latest_release_detailed()
    assert error is None
    assert rel["tag"] == "v0.1.24"
    # The bundle name is deterministic, so the download URL is recoverable.
    assert rel["zip_url"].endswith("/download/v0.1.24/family-office-0.1.24.zip")


def test_failure_reports_a_cause_the_user_can_act_on(monkeypatch):
    import httpx

    monkeypatch.setattr(httpx, "get", _responder({
        "github.com": httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] bad certificate"),
    }))
    rel, error = updater.latest_release_detailed()
    assert rel is None
    assert "TLS verification failed" in error

    monkeypatch.setattr(httpx, "get", _responder({
        "github.com": SimpleNamespace(status_code=403, text="API rate limit exceeded",
                                      headers={}),
    }))
    rel, error = updater.latest_release_detailed()
    assert rel is None and "rate-limited" in error


def test_status_carries_the_reason_and_a_manual_link(monkeypatch):
    monkeypatch.setattr(updater, "is_git_install", lambda: False)
    monkeypatch.setattr(updater, "latest_release_detailed",
                        lambda **kw: (None, "the connection timed out"))
    status = updater.get_update_status(fetch=True)
    assert status["latest"] is None
    assert status["error"] == "the connection timed out"
    assert status["releases_url"].endswith("/releases/latest")


def test_apply_surfaces_the_reason_instead_of_a_generic_message(monkeypatch):
    monkeypatch.setattr(updater, "latest_release_detailed",
                        lambda **kw: (None, "proxy error: 407 auth required"))
    ok, msg = updater.apply_manual_update()
    assert not ok and msg == "proxy error: 407 auth required"


# ── Install from a file the user downloaded ──────────────────────────────────

def test_upload_installs_a_bundle_without_touching_the_network(fake_install, tmp_path,
                                                               monkeypatch):
    """The escape hatch for a network the app can't reach: no HTTP at all."""
    import httpx

    root = fake_install
    monkeypatch.setattr(httpx, "get", _responder({}))  # any call would raise
    data = _make_bundle_zip(tmp_path / "handmade").read_bytes()

    ok, msg = updater.apply_bundle_file(data)
    assert ok, msg
    assert "updated to v9.9.9" in msg
    assert (root / "app" / "__init__.py").read_text() == "NEW"
    assert (root / "app" / "brand_new.py").exists()
    # User data is never in the blast radius.
    assert (root / "data" / "family_office.db").read_bytes() == b"PRECIOUS"
    assert (root / ".env").read_text() == "KFO_SECRET=1"
    assert (root / ".venv" / "marker").read_text() == "venv"


def test_upload_rejects_junk_and_downgrades(fake_install, tmp_path):
    ok, msg = updater.apply_bundle_file(b"")
    assert not ok and "empty" in msg

    not_a_bundle = tmp_path / "junk.zip"
    with zipfile.ZipFile(not_a_bundle, "w") as zf:
        zf.writestr("readme.txt", "hello")
    ok, msg = updater.apply_bundle_file(not_a_bundle.read_bytes())
    assert not ok and "isn't a Family Office bundle" in msg

    older = _make_bundle_zip(tmp_path / "older", version="0.1.17")
    ok, msg = updater.apply_bundle_file(older.read_bytes())
    assert not ok and "not newer" in msg
    # A rejected file leaves the install exactly as it was.
    assert (fake_install / "app" / "__init__.py").read_text() == "OLD"
