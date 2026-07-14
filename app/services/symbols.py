"""Ticker semantics — what we may send to a price feed, and in what form.

Shared by the pricing paths (market data, snapshots, backfill) and by the
symbol-repair service. Lives on its own so both can import it without a cycle.

Two jobs:
  • ``yahoo_symbol`` — the Yahoo-safe form of a ticker. Class shares are written
    a dozen ways across brokerages (``BRK/B``, ``BRK.B``); Yahoo wants ``BRK-B``.
    Sending the raw form yields "Failed to get ticker 'BRK/B'" on every refresh.
  • rejecting things that are not tickers at all — CUSIPs from a 401(k) export,
    and import artifacts like a subtotal row (``CRM-TOTAL``). These can never be
    priced, so querying them just burns a network round-trip and logs an error
    per refresh. ``unpriceable_assets`` surfaces them for the user to fix.
"""
from __future__ import annotations

import re

_CUSIP_RE = re.compile(r"^[A-Z0-9]{9}$")
# A real ticker: starts with a letter, then letters/digits, optionally one
# suffix group (BRK-B, BTC-USD, RDS-A). Length-capped — brokerage junk and
# subtotal rows blow past this.
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}(-[A-Z0-9]{1,4})?$")

# Suffixes brokerage exports append to subtotal / rollup rows. These arrive as
# "holdings" with a symbol like ``CRM-TOTAL`` and are not securities.
_ROLLUP_SUFFIXES = ("-TOTAL", "-SUBTOTAL", "-CASH", "-SUM")


def looks_like_cusip(symbol: str | None) -> bool:
    """A 9-character alphanumeric containing a digit — a CUSIP, not a ticker
    (tickers are short and almost never 9 chars with digits)."""
    s = (symbol or "").upper().strip()
    return bool(_CUSIP_RE.match(s)) and any(c.isdigit() for c in s)


def looks_like_rollup(symbol: str | None) -> bool:
    """True for an import artifact such as ``CRM-TOTAL`` — a broker subtotal
    line that got imported as if it were a position."""
    s = (symbol or "").upper().strip()
    return any(s.endswith(sfx) for sfx in _ROLLUP_SUFFIXES)


def yahoo_symbol(symbol: str | None) -> str | None:
    """The form Yahoo expects, or None when this can't be a tradeable ticker.

    Returning None is the important half: every caller skips the fetch instead of
    firing a request that is guaranteed to 404. Normalises separators so
    ``BRK/B`` and ``BRK.B`` both become ``BRK-B``.
    """
    s = (symbol or "").upper().strip()
    if not s:
        return None
    if looks_like_cusip(s) or looks_like_rollup(s):
        return None
    s = s.replace("/", "-").replace(".", "-")
    if not _TICKER_RE.match(s):
        return None
    return s
