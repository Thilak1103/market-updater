"""Earnings: who reported, how the market reacted, and the call transcripts.

- Calendar: Finnhub free (US, with EPS estimates and actuals) plus yfinance earnings dates
  for non-US watch-list names (refreshed weekly).
- Reaction: the day's move against the sector proxy and against the stock's average
  absolute move on its last eight earnings days.
- Transcripts: ROIC.ai free plan (latest two calls) then Alpha Vantage (US).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd

from ..db import DB
from ..util import get_with_retry, log
from . import fundamentals as F

FINNHUB = "https://finnhub.io/api/v1"


@dataclass
class Report:
    ticker: str
    date: str
    eps_actual: float | None = None
    eps_estimate: float | None = None
    rev_actual: float | None = None
    rev_estimate: float | None = None
    move_pct: float | None = None
    sector_move_pct: float | None = None
    avg_abs_move_pct: float | None = None
    transcript: str | None = None
    prev_transcript: str | None = None
    transcript_source: str | None = None
    hour: str | None = None            # "bmo" before open, "amc" after close, else unknown
    reaction_day: str | None = None    # the trading session whose close shows the reaction
    pending: bool = False              # reaction session hasn't closed yet
    notes: list[str] = field(default_factory=list)

    @property
    def eps_surprise_pct(self) -> float | None:
        if self.eps_actual is None or not self.eps_estimate:
            return None
        return (self.eps_actual / self.eps_estimate - 1) * 100 * (1 if self.eps_estimate > 0 else -1)

    @property
    def excess_move(self) -> float | None:
        if self.move_pct is None:
            return None
        return self.move_pct - (self.sector_move_pct or 0.0)


def finnhub_calendar(start: date, end: date) -> list[dict]:
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return []
    r = get_with_retry(f"{FINNHUB}/calendar/earnings",
                       params={"from": start.isoformat(), "to": end.isoformat(), "token": key})
    if r.status_code != 200:
        log.warning("Finnhub calendar -> %s", r.status_code)
        return []
    return r.json().get("earningsCalendar", []) or []


def refresh_yahoo_dates(db: DB, tickers: list[str]) -> None:
    """Store past and next earnings dates per ticker (run weekly)."""
    import yfinance as yf

    for t in tickers:
        try:
            df = yf.Ticker(t).get_earnings_dates(limit=12)
        except Exception as e:
            log.debug("earnings dates failed for %s: %s", t, e)
            continue
        if df is None or df.empty:
            continue
        rows = []
        for d in df.index:
            ts = pd.Timestamp(d)
            # yfinance gives exchange-local release times; afternoon releases react next day.
            hour = None if ts.hour == 0 and ts.minute == 0 else ("amc" if ts.hour >= 12 else "bmo")
            rows.append((t, ts.date().isoformat(), hour))
        with db.tx() as c:
            c.executemany("INSERT OR REPLACE INTO earnings_dates VALUES (?,?,?)", rows)


def _weekdays_back(d: date, n: int) -> date:
    while n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def reporters(db: DB, watchlist: list[str], day: date) -> list[Report]:
    """Watch-list companies that reported in the last two weekdays or today.

    Two days back so that an after-close report is still caught once its reaction session
    has closed; `add_reaction` and `reviewable` then decide which are ready for review.
    """
    start = _weekdays_back(day, 2)
    out: dict[str, Report] = {}
    wl = set(watchlist)
    for e in finnhub_calendar(start, day):
        sym = e.get("symbol")
        if sym in wl:
            out[sym] = Report(sym, e.get("date") or day.isoformat(), e.get("epsActual"),
                              e.get("epsEstimate"), e.get("revenueActual"), e.get("revenueEstimate"),
                              hour=e.get("hour") or None)
    for row in db.q("SELECT ticker, date, hour FROM earnings_dates WHERE date>=? AND date<=?",
                    (start.isoformat(), day.isoformat())):
        if row["ticker"] in wl and row["ticker"] not in out:
            out[row["ticker"]] = Report(row["ticker"], row["date"], hour=row["hour"])
    return list(out.values())


def _session(rep: Report) -> pd.Timestamp:
    d = pd.Timestamp(rep.date)
    if rep.hour == "amc":
        d += pd.offsets.BDay(1)
    return d.normalize()


def add_reaction(rep: Report, prices: pd.DataFrame, proxy: str | None, db: DB) -> None:
    if rep.ticker not in prices.columns:
        rep.pending = True
        return
    s = prices[rep.ticker].dropna()
    sess = _session(rep)
    if sess not in s.index:
        rep.pending = True      # reaction session not traded yet (e.g. a 07:00 UK release today)
        return
    i = s.index.get_loc(sess)
    if i < 1:
        rep.pending = True
        return
    rep.reaction_day = sess.date().isoformat()
    rep.move_pct = (float(s.iloc[i]) / float(s.iloc[i - 1]) - 1) * 100
    if proxy and proxy in prices.columns:
        p = prices[proxy].dropna()
        if sess in p.index and p.index.get_loc(sess) >= 1:
            j = p.index.get_loc(sess)
            rep.sector_move_pct = (float(p.iloc[j]) / float(p.iloc[j - 1]) - 1) * 100
    past = [r["date"] for r in db.q("SELECT date FROM earnings_dates WHERE ticker=? AND date<? "
                                    "ORDER BY date DESC LIMIT 8", (rep.ticker, rep.date))]
    moves = []
    for d in past:
        idx = s.index.searchsorted(pd.Timestamp(d))
        if 1 <= idx < len(s):
            # Release timing for old reports is unknown, so take the larger of the report
            # day's and the next day's absolute move.
            day_moves = [abs(float(s.iloc[k]) / float(s.iloc[k - 1]) - 1) * 100
                         for k in (idx, idx + 1) if k < len(s)]
            moves.append(max(day_moves))
    if moves:
        rep.avg_abs_move_pct = sum(moves) / len(moves)


def reviewable(rep: Report, prices: pd.DataFrame) -> bool:
    """Review each report once: on the first morning after its reaction session closes."""
    if rep.pending or not rep.reaction_day or rep.ticker not in prices.columns:
        return False
    last = prices[rep.ticker].dropna().index.max()
    return pd.Timestamp(rep.reaction_day) == last


def roic_transcripts(ticker: str, known_id: str | None) -> tuple[str | None, str | None]:
    for ident in ([known_id] if known_id else F.roic_candidates(ticker)):
        data = F._roic_get("/company-events", {"identifier": ident, "content": "transcript", "limit": 2})
        events = (data or {}).get("data") or []
        texts = []
        for ev in events[:2]:
            raw = next((t for t in ev.get("transcripts", []) if str(t.get("id", "")).startswith("trraw_")), None)
            if not raw:
                continue
            doc = F._roic_get(f"/documents/transcripts/{raw['id']}/raw")
            if doc and doc.get("text"):
                texts.append(doc["text"])
        if texts:
            return texts[0], (texts[1] if len(texts) > 1 else None)
    return None, None


def alpha_vantage_transcript(ticker: str, day: date) -> str | None:
    key = os.environ.get("ALPHAVANTAGE_API_KEY")
    if not key or "." in ticker:
        return None
    q = (day.month - 1) // 3 + 1
    candidates = [f"{day.year}Q{q}", f"{day.year}Q{q - 1}" if q > 1 else f"{day.year - 1}Q4"]
    for quarter in candidates:
        r = get_with_retry("https://www.alphavantage.co/query",
                           params={"function": "EARNINGS_CALL_TRANSCRIPT", "symbol": ticker,
                                   "quarter": quarter, "apikey": key}, tries=2)
        if r.status_code != 200:
            continue
        parts = r.json().get("transcript") or []
        if parts:
            return "\n".join(f"{p.get('speaker', '')} ({p.get('title', '')}): {p.get('content', '')}"
                             for p in parts)
    return None


def add_transcripts(rep: Report, db: DB, day: date) -> None:
    cached = F.get(db, rep.ticker)
    cur, prev = roic_transcripts(rep.ticker, cached.get("roic_id"))
    if cur:
        rep.transcript, rep.prev_transcript, rep.transcript_source = cur, prev, "ROIC.ai"
        return
    av = alpha_vantage_transcript(rep.ticker, day)
    if av:
        rep.transcript, rep.transcript_source = av, "Alpha Vantage"
    else:
        rep.notes.append("No transcript available yet; review uses results coverage only.")
