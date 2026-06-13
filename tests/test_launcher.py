"""Tests for the desktop launcher: master-password wiring and the port helper."""

import importlib.util
import socket
from pathlib import Path

from app.config import Settings

ROOT = Path(__file__).resolve().parent.parent


def _load_run():
    spec = importlib.util.spec_from_file_location("kfo_run", ROOT / "launchers" / "_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_find_free_port_returns_bindable_port():
    run = _load_run()
    port = run._find_free_port(8000)
    assert isinstance(port, int) and 1 <= port <= 65535
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", port))  # raises if not actually free
    s.close()


def test_master_password_localhost_encrypts_db_only():
    s = Settings(master_password="pw", host="127.0.0.1", db_passphrase="", access_code="")
    assert s.db_passphrase == "pw"   # DB encrypted
    assert s.access_code == ""        # no web gate on localhost


def test_master_password_network_adds_web_gate():
    s = Settings(master_password="pw", host="0.0.0.0", db_passphrase="", access_code="")
    assert s.db_passphrase == "pw"
    assert s.access_code == "pw"      # web login required when exposed


def test_explicit_values_win_over_master_password():
    s = Settings(master_password="pw", host="0.0.0.0", db_passphrase="x", access_code="y")
    assert s.db_passphrase == "x"
    assert s.access_code == "y"


def test_no_master_password_is_unchanged():
    s = Settings(master_password="", db_passphrase="", access_code="")
    assert s.db_passphrase == ""
    assert s.access_code == ""
