"""Tests for the PWA (web + offline) layer and the optional access-code gate."""

import pytest

from app.config import settings


# ── Progressive Web App shell ──────────────────────────────────────────────


def test_manifest_served_at_root(test_client):
    """The web manifest is served from root with the correct media type."""
    r = test_client.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/manifest+json"
    body = r.json()
    assert body["start_url"].startswith("/")
    assert body["display"] == "standalone"
    assert body["icons"], "manifest must declare at least one icon"


def test_service_worker_served_with_root_scope(test_client):
    """The service worker is served from root and allowed to control all of '/'."""
    r = test_client.get("/service-worker.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    # Header that lets a root-scoped SW control the whole origin.
    assert r.headers.get("service-worker-allowed") == "/"
    assert "addEventListener" in r.text


def test_offline_fallback_page(test_client):
    """The offline fallback page renders standalone (no network/framework needed)."""
    r = test_client.get("/offline")
    assert r.status_code == 200
    assert "You're offline" in r.text


def test_base_template_uses_vendored_assets_not_cdn(test_client):
    """The shell must load JS locally so it works with no internet."""
    html = test_client.get("/").text
    assert "/static/vendor/tailwind.min.js" in html
    assert "/static/vendor/htmx.min.js" in html
    assert "/static/vendor/chart.umd.min.js" in html
    # No external CDN script sources remain in the shell.
    assert "cdn.tailwindcss.com" not in html
    assert "unpkg.com" not in html
    assert 'rel="manifest"' in html
    assert "serviceWorker" in html  # registration script present


# ── Optional access-code gate ──────────────────────────────────────────────


@pytest.fixture
def gated():
    """Enable the shared-passcode gate for the duration of a test."""
    settings.access_code = "test-code"
    try:
        yield "test-code"
    finally:
        settings.access_code = ""


def test_open_by_default(test_client):
    """With no access code configured, the app is open (local-first default)."""
    assert settings.access_code == ""
    assert test_client.get("/").status_code == 200


def test_gate_redirects_browser_to_login(test_client, gated):
    """An unauthenticated browser navigation is redirected to /login."""
    r = test_client.get(
        "/portfolio/",
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/portfolio/"


def test_gate_401s_htmx_and_api(test_client, gated):
    """HTMX/fetch requests get a 401 rather than an HTML redirect."""
    r = test_client.get(
        "/",
        headers={"accept": "text/html", "hx-request": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 401


def test_pwa_shell_stays_open_when_gated(test_client, gated):
    """The shell endpoints must remain reachable so the app can boot offline."""
    for path in ["/service-worker.js", "/manifest.webmanifest", "/offline", "/health"]:
        assert test_client.get(path).status_code == 200, path


def test_login_flow_sets_session(test_client, gated):
    """Wrong code is rejected; correct code authenticates subsequent requests."""
    assert (
        test_client.post(
            "/login", data={"code": "wrong", "next": "/"}, follow_redirects=False
        ).status_code
        == 401
    )

    ok = test_client.post(
        "/login", data={"code": "test-code", "next": "/"}, follow_redirects=False
    )
    assert ok.status_code == 303
    assert "kfo_session" in ok.headers.get("set-cookie", "")
    # TestClient keeps the cookie — the protected page now loads.
    assert test_client.get("/", follow_redirects=False).status_code == 200


def test_login_rejects_open_redirect(test_client, gated):
    """A non-relative `next` is ignored to prevent open-redirect abuse."""
    r = test_client.post(
        "/login",
        data={"code": "test-code", "next": "https://evil.example/"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
