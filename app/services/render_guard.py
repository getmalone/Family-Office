"""
Render guard — keep page renders off the network.

The cardinal rule for responsiveness: **a web request must never make a
synchronous, portfolio-sized network call.** With hundreds of holdings, a single
per-asset yfinance fetch on render turns a page load into 20–40 seconds (and, on
the PWA, dumps the user to the "You're offline" screen when a fetch hangs).

So incidental price/regime fetches are gated: during a normal render
``network_allowed()`` is False and those code paths fall back to stored data.
Network access is opted into explicitly — and only — around the deliberate
refresh paths (``/prices/refresh``, backfill, snapshot capture, the morning
brief's force-refresh) via ``with allow_network(): ...``.

Implemented with a ``ContextVar`` so the flag is correct under threads and async
without leaking between requests.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar

_allow_network: ContextVar[bool] = ContextVar("kfo_allow_network", default=False)


def network_allowed() -> bool:
    """True only inside an ``allow_network()`` block — i.e. an explicit refresh.
    Incidental fetchers check this and stay DB-only during ordinary renders."""
    return _allow_network.get()


@contextlib.contextmanager
def allow_network():
    """Permit deliberate network fetching for the duration of the block."""
    token = _allow_network.set(True)
    try:
        yield
    finally:
        _allow_network.reset(token)
