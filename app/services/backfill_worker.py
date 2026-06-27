"""One-shot background price-history backfill.

The trailing-window Performance panel (3M / 6M / 12M / YTD) needs a real price
*history* — what each holding was worth on past dates. A CSV import can't supply
that (it's a single-day snapshot of holdings + cost), and page renders never go
to the network (see :mod:`app.services.render_guard`). So we populate the
history out-of-band: a single guarded background thread downloads ~2 years of
daily closes, stores them, and busts the relevant caches. The panel then fills
in on the next dashboard load.

Triggered three ways, all non-blocking:
  • at app startup (catch up an already-imported portfolio),
  • right after a CSV import (so a fresh import populates), and
  • lazily from the dashboard when it notices the history is still thin.

A module-level guard ensures only one runs at a time, and a cooldown stops it
from re-running in a tight loop once it has done its work (or found nothing to
fetch — e.g. a portfolio of CITs with no public history).
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

from app.services.db import get_db_session, get_factory
from app.services.market_data import MarketDataService
from app.services.render_guard import allow_network

_lock = threading.Lock()
_running = False
_last_finished: datetime | None = None
_last_result: dict | None = None

# Don't re-attempt more often than this once a run has finished. Keeps the lazy
# dashboard trigger from hammering yfinance when history stays thin (e.g. a
# portfolio whose symbols have no public price history).
_COOLDOWN = timedelta(minutes=20)


def is_running() -> bool:
    return _running


def status() -> dict:
    """Lightweight snapshot for the UI ('building price history…')."""
    return {
        "running": _running,
        "last_finished": _last_finished.isoformat() if _last_finished else None,
        "last_result": _last_result,
    }


def run_backfill_now(session=None) -> dict:
    """Do the backfill synchronously (used by the worker thread and by tests).

    Only fetches when history is thin; always inside an ``allow_network()`` block
    so the deliberate download is permitted while ordinary renders stay DB-only.
    Busts the period-returns and morning-brief caches so the new history shows.
    """
    own = session is None
    ctx = get_db_session(get_factory()) if own else _null_ctx(session)
    with ctx as s:
        md = MarketDataService(s)
        if not md.history_is_thin():
            return {"skipped": "history already deep"}
        with allow_network():
            result = md.backfill_history()
            try:
                md.refresh_proxy_prices()
            except Exception:
                pass
    _invalidate_caches()
    return result


def ensure_history(force: bool = False) -> bool:
    """Kick a background backfill if one isn't already running (and we're past
    the cooldown). Returns True if a job was started. Cheap to call on every
    dashboard load — the thin-check and download happen on the worker thread, so
    the request thread never blocks."""
    global _running
    now = datetime.now()
    with _lock:
        if _running:
            return False
        if not force and _last_finished and (now - _last_finished) < _COOLDOWN:
            return False
        _running = True

    def _worker():
        global _running, _last_finished, _last_result
        try:
            _last_result = run_backfill_now()
        except Exception as exc:  # never let a background failure escape
            _last_result = {"error": str(exc)}
        finally:
            _last_finished = datetime.now()
            with _lock:
                _running = False

    threading.Thread(target=_worker, name="kfo-backfill", daemon=True).start()
    return True


def _invalidate_caches() -> None:
    try:
        from app.services.analysis_service import invalidate_period_returns_cache
        invalidate_period_returns_cache()
    except Exception:
        pass
    try:
        from app.services.morning_brief_service import invalidate_brief_cache
        invalidate_brief_cache()
    except Exception:
        pass


class _null_ctx:
    """Wrap a caller-supplied session so run_backfill_now can use one `with`."""

    def __init__(self, session):
        self._s = session

    def __enter__(self):
        return self._s

    def __exit__(self, *exc):
        return False
