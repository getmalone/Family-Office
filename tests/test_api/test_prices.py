"""POST /prices/refresh — the endpoint that 500'd in the v0.1.24 logs.

The Refresh button raced the Daily Brief's background capture for SQLite's single
write lock, lost it after busy_timeout, and the un-rolled-back failure took the
whole request down with "database is locked".
"""


def test_refresh_reports_success(test_client):
    r = test_client.post("/prices/refresh")
    assert r.status_code == 200


def test_refresh_stays_up_when_a_capture_is_already_running(test_client, monkeypatch):
    """A Refresh pressed mid-capture returns the readings on hand — it must not
    queue up as a second writer and it must not 500."""
    from app.services import snapshot_service as ss

    monkeypatch.setattr(ss, "_LOCK_WAIT_SECONDS", 0.05)   # don't make the test wait

    assert ss._capture_lock.acquire(blocking=False)        # a capture is in flight
    try:
        r = test_client.post("/prices/refresh")
    finally:
        ss._capture_lock.release()

    assert r.status_code == 200
    assert r.json()["skipped"] == "capture already running"


def test_refresh_survives_a_locked_database(test_client, monkeypatch):
    """Even if the write burst itself loses the lock, the response is a plain
    'try again' — not a traceback — and the session is left usable."""
    from sqlalchemy.exc import OperationalError

    from app.services.snapshot_service import SnapshotService

    def _locked(self, *a, **k):
        raise OperationalError("UPDATE price_snapshots", {}, Exception("database is locked"))

    monkeypatch.setattr(SnapshotService, "_capture_now", _locked)

    r = test_client.post("/prices/refresh")
    assert r.status_code == 503
    assert "try again" in r.json()["error"]
