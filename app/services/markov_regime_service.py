"""
Markov Chain Regime Detection for the Family Office.

Detects the current market regime (Bull / Sideways / Bear) from price history
using a discrete-time Markov chain fitted to a labelled return sequence.

Algorithm
---------
1. Fetch close-price history via yfinance (or accept a pre-built array).
2. Compute the 20-day rolling cumulative return for each trading day.
3. Label each day:
       Bull     > +5% cumulative
       Bear     < -5% cumulative
       Sideways otherwise
4. Build a 3×3 transition matrix T with Laplace smoothing (ε = 0.01).
   T[i, j] = P(tomorrow in state j | today in state i).
5. Current state = label of the most recent day.
6. One-step  distribution: e_i  @  T
7. Five-step distribution: e_i  @  T^5
8. Stationary distribution: dominant left eigenvector of T (right eigenvector of Tᵀ).
9. Signal score = π_Bull − π_Bear   (−1.0 → +1.0).

References
----------
Hamilton, J.D. (1989) "A New Approach to the Economic Analysis of
Nonstationary Time Series and the Business Cycle", Econometrica 57(2).
"""

from __future__ import annotations

from datetime import date

import numpy as np

from app.schemas.analysis import RegimeState, RegimeTransition
from app.services.symbols import yahoo_symbol

# ── Constants ──────────────────────────────────────────────────────────────────

STATES: list[str] = ["Bull", "Sideways", "Bear"]
STATE_IDX: dict[str, int] = {s: i for i, s in enumerate(STATES)}

BULL_THRESHOLD = 0.05    # +5% cumulative over WINDOW days → Bull
BEAR_THRESHOLD = -0.05   # −5% cumulative over WINDOW days → Bear
WINDOW = 20              # rolling look-back in trading days
LAPLACE_EPS = 0.01       # Laplace smoothing to avoid zero-probability transitions


# ── Service ───────────────────────────────────────────────────────────────────

class MarkovRegimeService:
    """
    Fit and query a Markov chain regime model for any ticker.

    The class is stateless (no DB dependency) — all state lives in the
    price array passed in or fetched from yfinance.

    Example
    -------
    svc = MarkovRegimeService()
    state = svc.get_regime("SPY")          # 2-year look-back by default
    batch = svc.get_regime_batch(["SPY", "QQQ", "AAPL"])
    """

    # ── Public API ────────────────────────────────────────────────────────────

    def get_regime(
        self,
        symbol: str,
        lookback_days: int = 504,
        prices: np.ndarray | None = None,
    ) -> RegimeState | None:
        """
        Compute the current Markov regime state for *symbol*.

        Parameters
        ----------
        symbol : str
            Ticker to analyse (used for yfinance fetch if *prices* is None).
        lookback_days : int
            Trading-day history to fetch from yfinance (default ≈ 2 years).
        prices : np.ndarray | None
            Pre-fetched close-price array (oldest first).  Skip yfinance if supplied.

        Returns
        -------
        RegimeState, or None if there is insufficient data.
        """
        if prices is None:
            prices = self._fetch_prices(symbol, lookback_days)
        if prices is None or len(prices) < WINDOW + 10:
            return None

        labels = self._label_sequence(prices)
        if len(labels) < 5:
            return None

        T = self._build_transition_matrix(labels)
        current = labels[-1]
        ci = STATE_IDX[current]

        e_i = np.zeros(3)
        e_i[ci] = 1.0

        one_step_arr  = e_i @ T
        five_step_arr = e_i @ np.linalg.matrix_power(T, 5)
        stat_arr      = self._stationary(T)

        signal = float(stat_arr[STATE_IDX["Bull"]] - stat_arr[STATE_IDX["Bear"]])

        return RegimeState(
            symbol=symbol,
            current_regime=current,
            persist_pct=round(float(T[ci, ci]) * 100, 1),
            one_step=RegimeTransition(
                bull=round(float(one_step_arr[0]), 4),
                sideways=round(float(one_step_arr[1]), 4),
                bear=round(float(one_step_arr[2]), 4),
            ),
            five_step=RegimeTransition(
                bull=round(float(five_step_arr[0]), 4),
                sideways=round(float(five_step_arr[1]), 4),
                bear=round(float(five_step_arr[2]), 4),
            ),
            stationary=RegimeTransition(
                bull=round(float(stat_arr[0]), 4),
                sideways=round(float(stat_arr[1]), 4),
                bear=round(float(stat_arr[2]), 4),
            ),
            signal_score=round(signal, 4),
            lookback_days=lookback_days,
            as_of=date.today(),
        )

    def get_portfolio_and_ticker_regimes(
        self,
        symbols: list[str],
        weights: dict[str, float],
    ) -> tuple["RegimeState | None", "dict[str, RegimeState | None]"]:
        """
        Single yfinance download that returns both a portfolio-blended regime
        and per-ticker regime states.

        The portfolio regime is computed from the market-value-weighted composite
        daily return series, so it reflects the actual mix of your holdings — not
        just what the S&P 500 is doing.

        Parameters
        ----------
        symbols  : publicly-traded tickers in the portfolio
        weights  : market-value weight for each symbol (any positive scale; will
                   be normalised internally so they don't have to sum to 1)

        Returns
        -------
        (portfolio_regime, per_ticker_dict)
            portfolio_regime  — RegimeState with symbol="Portfolio", or None
            per_ticker_dict   — {symbol: RegimeState | None} for each input symbol
        """
        if not symbols:
            return None, {}

        ticker_regimes: dict[str, RegimeState | None] = {s: None for s in symbols}
        ticker_returns: dict[str, np.ndarray] = {}

        # Fetch by the Yahoo-safe form and drop the unpriceable (a CUSIP from a
        # 401(k) import, a subtotal row). Sending them raw meant every call
        # errored per junk symbol — and holdings stored as BRK/B never priced at
        # all, because Yahoo only answers for BRK-B. Results stay keyed by the
        # caller's original symbols.
        originals_by_ysym = self._yahoo_map(symbols)
        if not originals_by_ysym:
            return None, ticker_regimes

        try:
            import yfinance as yf
            fetch_syms = list(originals_by_ysym)
            raw = yf.download(
                fetch_syms,
                period="2y",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            if raw.empty:
                return None, ticker_regimes

            close = raw["Close"] if "Close" in raw.columns else raw

            for ysym, origs in originals_by_ysym.items():
                try:
                    if hasattr(close, "columns"):
                        if ysym not in close.columns:
                            continue
                        col = close[ysym].dropna()
                    else:
                        # Single-ticker edge case
                        col = close.dropna()

                    if len(col) < WINDOW + 10:
                        continue

                    prices = np.array(col.values, dtype=float)
                    rets = np.diff(prices) / prices[:-1]

                    for sym in origs:
                        # Per-ticker regime
                        ticker_regimes[sym] = self.get_regime(sym, prices=prices)
                        # Save daily returns for portfolio blending
                        ticker_returns[sym] = rets

                except Exception:
                    continue

        except Exception:
            return None, ticker_regimes

        # Portfolio-blended regime
        portfolio_regime = self._blend_portfolio_regime(ticker_returns, weights)
        return portfolio_regime, ticker_regimes

    def portfolio_and_ticker_regimes_from_prices(
        self,
        price_map: "dict[str, np.ndarray]",
        weights: dict[str, float],
    ) -> tuple["RegimeState | None", "dict[str, RegimeState | None]"]:
        """Same result as :meth:`get_portfolio_and_ticker_regimes`, but computed
        entirely from pre-loaded close-price arrays (e.g. our stored 2-year
        history) — **no network**. This is what page renders use; the live
        download variant is reserved for explicit refreshes.

        ``price_map`` maps symbol → close prices (oldest first, NaNs removed).
        """
        ticker_regimes: dict[str, RegimeState | None] = {s: None for s in price_map}
        ticker_returns: dict[str, np.ndarray] = {}

        for sym, prices in price_map.items():
            try:
                if prices is None or len(prices) < WINDOW + 10:
                    continue
                arr = np.asarray(prices, dtype=float)
                ticker_regimes[sym] = self.get_regime(sym, prices=arr)
                ticker_returns[sym] = np.diff(arr) / arr[:-1]
            except Exception:
                continue

        portfolio_regime = self._blend_portfolio_regime(ticker_returns, weights)
        return portfolio_regime, ticker_regimes

    def get_regime_batch(
        self,
        symbols: list[str],
        lookback_days: int = 504,
    ) -> dict[str, RegimeState | None]:
        """
        Compute regime states for multiple symbols with a single yfinance download.

        Returns a dict mapping symbol → RegimeState (or None if data is insufficient).
        Symbols that are not found in yfinance are mapped to None.
        """
        if not symbols:
            return {}

        results: dict[str, RegimeState | None] = {s: None for s in symbols}
        originals_by_ysym = self._yahoo_map(symbols)
        if not originals_by_ysym:
            return results
        try:
            import yfinance as yf
            raw = yf.download(
                list(originals_by_ysym),
                period="2y",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
            if raw.empty:
                return results

            close = raw["Close"] if "Close" in raw.columns else raw

            for ysym, origs in originals_by_ysym.items():
                try:
                    if hasattr(close, "columns"):
                        if ysym not in close.columns:
                            continue
                        col = close[ysym].dropna()
                    else:
                        # Single-ticker download returns a Series, not a DataFrame
                        col = close.dropna()

                    if len(col) < WINDOW + 10:
                        continue

                    prices = np.array(col.values, dtype=float)
                    for sym in origs:
                        results[sym] = self.get_regime(
                            sym,
                            lookback_days=lookback_days,
                            prices=prices,
                        )
                except Exception:
                    continue

        except Exception:
            pass

        return results

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _yahoo_map(symbols: list[str]) -> "dict[str, list[str]]":
        """Yahoo-safe fetch symbol → the original symbols it answers for.

        ``BRK/B`` and ``BRK.B`` both fetch as ``BRK-B`` (one download, both keys
        get results); CUSIPs and other junk normalise to None and are dropped.
        """
        out: dict[str, list[str]] = {}
        for sym in symbols:
            ysym = yahoo_symbol(sym)
            if ysym is not None:
                out.setdefault(ysym, []).append(sym)
        return out

    def _blend_portfolio_regime(
        self,
        ticker_returns: dict[str, np.ndarray],
        weights: dict[str, float],
    ) -> "RegimeState | None":
        """
        Build a portfolio-weighted return series and run the Markov chain on it.

        Steps:
          1. Intersect tickers that have both returns AND a positive weight.
          2. Align to the shortest common return series.
          3. Compute weighted-average daily return at each time step.
          4. Reconstruct a synthetic price series (starts at 100) and call get_regime().
        """
        # Symbols with usable data and a nonzero weight
        valid = [
            (sym, ticker_returns[sym], weights.get(sym, 0.0))
            for sym in ticker_returns
            if weights.get(sym, 0.0) > 0 and len(ticker_returns[sym]) >= WINDOW + 5
        ]
        if not valid:
            return None

        # Normalize weights to sum to 1 over *included* symbols only
        total_w = sum(w for _, _, w in valid)
        if total_w <= 0:
            return None

        min_len = min(len(rets) for _, rets, _ in valid)
        port_rets = np.zeros(min_len)
        for sym, rets, w in valid:
            port_rets += (w / total_w) * rets[-min_len:]

        # Reconstruct price series from returns
        port_prices = np.empty(min_len + 1)
        port_prices[0] = 100.0
        for i in range(min_len):
            port_prices[i + 1] = port_prices[i] * (1.0 + port_rets[i])

        n_positions = len(valid)
        return self.get_regime(
            f"Portfolio ({n_positions} positions)",
            lookback_days=min_len,
            prices=port_prices,
        )

    def _fetch_prices(self, symbol: str, lookback_days: int) -> np.ndarray | None:
        """Fetch close prices for a single symbol via yfinance."""
        ysym = yahoo_symbol(symbol)
        if ysym is None:
            return None
        try:
            import yfinance as yf
            ticker = yf.Ticker(ysym)
            hist = ticker.history(period="2y")
            if hist.empty or len(hist) < WINDOW + 10:
                return None
            return np.array(hist["Close"].values, dtype=float)
        except Exception:
            return None

    def _label_sequence(self, prices: np.ndarray) -> list[str]:
        """
        Label each trading day using a *WINDOW*-day rolling cumulative return.

        For day t (t ≥ WINDOW):
            cum_return = (prices[t] − prices[t − WINDOW]) / prices[t − WINDOW]
            Bull     if cum_return > BULL_THRESHOLD
            Bear     if cum_return < BEAR_THRESHOLD
            Sideways otherwise

        Returns labels from day WINDOW onward (len = len(prices) − WINDOW).
        """
        labels: list[str] = []
        for t in range(WINDOW, len(prices)):
            r = (prices[t] - prices[t - WINDOW]) / prices[t - WINDOW]
            if r > BULL_THRESHOLD:
                labels.append("Bull")
            elif r < BEAR_THRESHOLD:
                labels.append("Bear")
            else:
                labels.append("Sideways")
        return labels

    def _build_transition_matrix(self, labels: list[str]) -> np.ndarray:
        """
        Count state-to-state transitions; apply Laplace smoothing (ε = 0.01);
        then row-normalise so each row sums to 1.

        T[i, j]  =  P(next state = STATES[j]  |  current state = STATES[i])
        """
        counts = np.full((3, 3), LAPLACE_EPS)
        for t in range(len(labels) - 1):
            i = STATE_IDX[labels[t]]
            j = STATE_IDX[labels[t + 1]]
            counts[i, j] += 1
        row_sums = counts.sum(axis=1, keepdims=True)
        return counts / row_sums

    def _stationary(self, T: np.ndarray) -> np.ndarray:
        """
        Compute the stationary distribution π satisfying π T = π.

        Method: find the left eigenvector of T corresponding to eigenvalue 1,
        which equals the right eigenvector of Tᵀ.

        Falls back to power iteration (1 000 steps) on numerical failure.
        """
        try:
            eigenvalues, eigenvectors = np.linalg.eig(T.T)
            idx = int(np.argmin(np.abs(eigenvalues - 1.0)))
            v = eigenvectors[:, idx].real
            v = np.abs(v)
            total = v.sum()
            if total > 0 and not np.any(np.isnan(v)):
                v = v / total
                if np.all(v >= 0):
                    return v
        except Exception:
            pass

        # Power-iteration fallback
        pi = np.ones(3) / 3.0
        for _ in range(1000):
            pi_new = pi @ T
            if np.max(np.abs(pi_new - pi)) < 1e-10:
                break
            pi = pi_new
        return pi
