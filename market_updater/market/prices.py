"""Price history and the morning snapshot. yfinance first, Stooq as a cross-check.

Numbers in the briefing come only from here (never from the language model).
"""
from __future__ import annotations

import io
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pandas as pd

from ..db import DB
from ..util import get_with_retry, log


@dataclass
class Quote:
    ticker: str
    last: float
    prev: float
    asof: str            # ISO date of the last bar
    cross_checked: bool | None = None   # True ok, False disagreed, None not checked

    @property
    def change_pct(self) -> float:
        return (self.last / self.prev - 1) * 100 if self.prev else 0.0


def yahoo_history(tickers: list[str], period: str = "2y") -> pd.DataFrame:
    """Daily closes, columns = tickers. Missing tickers are simply absent."""
    import yfinance as yf

    if not tickers:
        return pd.DataFrame()
    df = yf.download(tickers, period=period, interval="1d", auto_adjust=True, progress=False,
                     group_by="column", threads=True)
    if df.empty:
        return pd.DataFrame()
    closes = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]].rename(
        columns={"Close": tickers[0]})
    if isinstance(closes, pd.Series):
        closes = closes.to_frame(tickers[0])
    closes.index = pd.to_datetime(closes.index).tz_localize(None)
    return closes.dropna(how="all")


# Stooq symbols for the cross-check (indices need explicit mapping).
STOOQ_INDEX = {
    "^GSPC": "^spx", "^NDX": "^ndx", "^FTSE": "^ukx", "^GDAXI": "^dax", "^N225": "^nkx",
    "^HSI": "^hsi", "^STOXX50E": "^sx5e", "^KS11": "^kospi",
}
STOOQ_SUFFIX = {".L": ".uk", ".DE": ".de", ".T": ".jp", ".HK": ".hk", ".PA": ".fr"}


def stooq_symbol(ticker: str) -> str | None:
    if ticker in STOOQ_INDEX:
        return STOOQ_INDEX[ticker]
    if ticker.startswith("^") or "=" in ticker:
        return None
    for suf, rep in STOOQ_SUFFIX.items():
        if ticker.endswith(suf):
            return ticker[: -len(suf)].lower() + rep
    if "." not in ticker:
        return ticker.lower() + ".us"
    return None


def stooq_last_close(symbol: str) -> tuple[str, float] | None:
    r = get_with_retry("https://stooq.com/q/d/l/", params={"s": symbol, "i": "d"}, tries=1)
    if r.status_code != 200 or "Date" not in r.text[:50]:
        return None
    df = pd.read_csv(io.StringIO(r.text))
    if df.empty:
        return None
    row = df.iloc[-1]
    return str(row["Date"]), float(row["Close"])


class Prices:
    """Price access with a SQLite cache. Inject `fetch` in tests."""

    def __init__(self, db: DB, fetch=yahoo_history, cross_check: bool = True,
                 disagreement_pct: float = 0.5):
        self.db = db
        self.fetch = fetch
        self.cross_check = cross_check
        self.disagreement_pct = disagreement_pct
        self._frame: pd.DataFrame | None = None
        # The cross-check is a nicety: cap it so a slow Stooq can't stall the run.
        self._checks: dict[str, bool | None] = {}
        self._check_failures = 0
        self._check_budget_s = 90.0

    def load(self, tickers: list[str], period: str = "2y") -> pd.DataFrame:
        tickers = sorted(set(t for t in tickers if t))
        df = self.fetch(tickers, period)
        if not df.empty:
            rows = [(t, d.date().isoformat(), float(v)) for t in df.columns
                    for d, v in df[t].dropna().items()]
            with self.db.tx() as c:
                c.executemany("INSERT OR REPLACE INTO prices VALUES (?,?,?)", rows)
        missing = [t for t in tickers if t not in df.columns]
        if missing:
            log.warning("No prices for %s", ", ".join(missing))
        self._frame = df
        return df

    @property
    def frame(self) -> pd.DataFrame:
        return self._frame if self._frame is not None else pd.DataFrame()

    def quote(self, ticker: str) -> Quote | None:
        df = self.frame
        if ticker not in df.columns:
            return None
        s = df[ticker].dropna()
        if len(s) < 2:
            return None
        q = Quote(ticker, float(s.iloc[-1]), float(s.iloc[-2]), s.index[-1].date().isoformat())
        if self.cross_check:
            q.cross_checked = self._cross_check(ticker, q)
        return q

    def _cross_check(self, ticker: str, q: "Quote") -> bool | None:
        if ticker in self._checks:
            return self._checks[ticker]
        sym = stooq_symbol(ticker)
        if not sym or self._check_failures >= 3 or self._check_budget_s <= 0:
            return None
        t0 = time.monotonic()
        result = None
        try:
            got = stooq_last_close(sym)
            if got and got[0] == q.asof:
                result = abs(got[1] / q.last - 1) * 100 <= self.disagreement_pct
            elif not got:
                self._check_failures += 1
        except Exception as e:
            self._check_failures += 1
            log.debug("Stooq check failed for %s: %s", ticker, e)
        self._check_budget_s -= time.monotonic() - t0
        if self._check_failures == 3:
            log.warning("Stooq cross-check switched off for this run after 3 failures")
        self._checks[ticker] = result
        return result

    def returns(self, ticker: str, days: int) -> float | None:
        df = self.frame
        if ticker not in df.columns:
            return None
        s = df[ticker].dropna()
        if len(s) <= days:
            return None
        return (float(s.iloc[-1]) / float(s.iloc[-1 - days]) - 1) * 100

    def ytd(self, ticker: str) -> float | None:
        df = self.frame
        if ticker not in df.columns:
            return None
        s = df[ticker].dropna()
        this_year = s[s.index.year == s.index[-1].year]
        prior = s[s.index.year < s.index[-1].year]
        if prior.empty or this_year.empty:
            return None
        return (float(s.iloc[-1]) / float(prior.iloc[-1]) - 1) * 100

    def close_on(self, ticker: str, day: str) -> float | None:
        row = self.db.one("SELECT close FROM prices WHERE ticker=? AND date<=? ORDER BY date DESC LIMIT 1",
                          (ticker, day))
        return float(row["close"]) if row else None
