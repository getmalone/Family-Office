"""Data lives outside the install folder; updates only touch changed files and
restart the app in place when the desktop launcher is running it."""

import sys
from pathlib import Path

import pytest

from app import paths
from app.services import restart, updater


# ── Data folder ─────────────────────────────────────────────────────────────

def test_data_home_override_and_platform_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("KFO_DATA_DIR", str(tmp_path / "custom"))
    assert paths.data_home() == tmp_path / "custom"
    monkeypatch.delenv("KFO_DATA_DIR")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert paths.data_home() == Path.home() / "Library" / "Application Support" / "FamilyOffice"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths.data_home() == tmp_path / "xdg" / "FamilyOffice"


def _legacy_install(root: Path):
    (root / "data" / "backups").mkdir(parents=True)
    (root / "data" / "family_office.db").write_bytes(b"encrypted-db")
    (root / "data" / "backups" / "family_office-20260101.db").write_bytes(b"old")
    (root / ".env").write_text("KFO_ANTHROPIC_API_KEY=sk-test\n")


def test_legacy_data_moved_once(monkeypatch, tmp_path):
    monkeypatch.delenv("KFO_DB_PATH", raising=False)
    home, root = tmp_path / "home", tmp_path / "app"
    monkeypatch.setenv("KFO_DATA_DIR", str(home))
    _legacy_install(root)

    done = paths.migrate_legacy_data(root)
    assert done and (home / "family_office.db").read_bytes() == b"encrypted-db"
    assert (home / "backups" / "family_office-20260101.db").exists()
    assert (home / ".env").read_text().startswith("KFO_ANTHROPIC_API_KEY")
    assert not (root / "data").exists() and not (root / ".env").exists()
    assert (root / ".env.moved").exists()
    assert any(p.name.startswith("data.moved-") for p in root.iterdir())
    assert paths.migrate_legacy_data(root) == []                # second launch: nothing to do


def test_no_move_when_data_folder_already_has_a_database(monkeypatch, tmp_path):
    monkeypatch.delenv("KFO_DB_PATH", raising=False)
    home, root = tmp_path / "home", tmp_path / "app"
    monkeypatch.setenv("KFO_DATA_DIR", str(home))
    home.mkdir()
    (home / "family_office.db").write_bytes(b"current")
    _legacy_install(root)
    assert paths.migrate_legacy_data(root) == []
    assert (home / "family_office.db").read_bytes() == b"current"
    assert (root / "data" / "family_office.db").exists()         # left alone


def test_settings_default_to_data_folder(monkeypatch, tmp_path):
    monkeypatch.delenv("KFO_DB_PATH", raising=False)
    monkeypatch.setenv("KFO_DATA_DIR", str(tmp_path))
    from app.config import Settings
    assert Path(Settings().db_path) == tmp_path / "family_office.db"


# ── Updates touch only changed files ────────────────────────────────────────

def test_sync_tree_leaves_unchanged_files_alone(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    for d in (src, dst):
        (d / "pkg").mkdir(parents=True)
    (src / "launcher.command").write_text("same")
    (dst / "launcher.command").write_text("same")
    (src / "pkg" / "mod.py").write_text("new")
    (dst / "pkg" / "mod.py").write_text("old")
    (dst / "pkg" / "gone.py").write_text("removed in new version")
    (dst / "pkg" / "__pycache__").mkdir()
    (dst / "pkg" / "__pycache__" / "x.pyc").write_bytes(b"c")
    before = (dst / "launcher.command").stat()

    updater._sync_tree(src, dst)

    after = (dst / "launcher.command").stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert (dst / "pkg" / "mod.py").read_text() == "new"
    assert not (dst / "pkg" / "gone.py").exists()
    assert (dst / "pkg" / "__pycache__" / "x.pyc").exists()


# ── Restart after update ───────────────────────────────────────────────────

@pytest.fixture
def launcher_hook():
    calls = []
    restart.register(lambda: calls.append("restart"))
    yield calls
    restart.register(None)


def test_request_restart_without_launcher_is_a_no_op():
    restart.register(None)
    assert restart.request_restart() is False


def test_update_triggers_restart_when_launcher_running(test_client, monkeypatch, launcher_hook):
    import time
    monkeypatch.setattr(updater, "get_update_status",
                        lambda fetch=True: {"update_available": True, "is_git": False, "latest": "v9.9.9"})
    monkeypatch.setattr(updater, "apply_manual_update", lambda: (True, "updated to v9.9.9"))
    r = test_client.post("/settings/apply-update")
    assert r.json()["restarting"] is True and r.json()["restart_required"] is False
    time.sleep(1.8)                                               # restart is scheduled after the response
    assert launcher_hook == ["restart"]
