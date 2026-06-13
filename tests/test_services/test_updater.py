"""Tests for version parsing and update-status reporting."""

from app.services.updater import _parse, get_update_status
from app.version import get_app_version


def test_version_is_nonempty():
    assert get_app_version()


def test_parse_orders_versions():
    assert _parse("v0.1.2") > _parse("0.1.1")
    assert _parse("1.0.0") > _parse("0.9.9")
    assert _parse("v0.1.1") == _parse("0.1.1")
    assert _parse("0.2.0") > _parse("0.1.9")


def test_update_status_shape():
    s = get_update_status(fetch=False)
    assert {"current", "latest", "update_available", "is_git"} <= set(s)
    assert isinstance(s["update_available"], bool)
    assert s["current"] == get_app_version()
