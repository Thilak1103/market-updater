"""Company fundamentals for comps: ROIC.ai free plan first, yfinance for the rest.

Fundamentals change quarterly, so results are cached and refreshed weekly or after a report.
Every metric records where it came from.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from typing import Any

from ..db import DB
from ..util import get_with_retry, log

ROIC = "https://api.roic.ai/v3.0.0"
# Guesses for ROIC's EXCHANGE:SYMBOL identifiers, tried in order and cached once one works.
ROIC_EXCHANGES = {
    ".L": ["LSE", "LON"], ".DE": ["XETRA", "ETR", "FRA"], ".PA": ["EPA", "PAR"], ".AS": ["AMS"],
    ".MI": ["BIT", "MIL"], ".MC": ["BME", "MCE"], ".SW": ["SWX", "SIX"], ".HK": ["HKG", "HKEX"],
    ".T": ["TYO", "TSE"],
}


class RoicLimiter:
    """Free plan: 5 requests a minute."""

    def __init__(self, per_minute: int = 5):
        self.gap = 60.0 / per_minute + 0.5
        self.last = 0.0

    def wait(self):
        d = self.gap - (time.time() - self.last)
        if d > 0:
            time.sleep(d)
        self.last = time.time()


_limiter = RoicLimiter()


def _roic_get(path: str, params: dict | None = None) -> dict | None:
    key = os.environ.get("ROIC_API_KEY")
    if not key:
        return None
    _limiter.wait()
    p = {"apikey": key, **(params or {})}
    r = get_with_retry(ROIC + path, params=p, tries=2)
    if r.status_code != 200:
        return None
    return r.json()


def roic_candidates(ticker: str) -> list[str]:
    for suf, exchanges in ROIC_EXCHANGES.items():
        if ticker.endswith(suf):
            sym = ticker[: -len(suf)]
            return [f"{ex}:{sym}" for ex in exchanges]
    if "." not in ticker and not ticker.startswith("^"):
        return [ticker, f"NASDAQ:{ticker}", f"NYSE:{ticker}"]
    return []


def _latest(data: dict | None) -> dict:
    rows = (data or {}).get("data") or []
    return rows[0] if rows else {}


def roic_fundamentals(ticker: str, known_id: str | None = None) -> tuple[dict, str | None]:
    ids = [known_id] if known_id else roic_candidates(ticker)
    for ident in ids:
        mult = _latest(_roic_get(f"/fundamental/multiples/{ident}", {"period_type": "ttm", "limit": 1}))
        if not mult:
            continue
        prof = _latest(_roic_get(f"/fundamental/ratios/profitability/{ident}", {"period_type": "ttm", "limit": 1}))
        cred = _latest(_roic_get(f"/fundamental/ratios/credit/{ident}", {"period_type": "ttm", "limit": 1}))
        out = {
            "ev": mult.get("enterprise_value"), "ev_sales": mult.get("ev_to_ttm_sales"),
            "ev_ebitda": mult.get("ev_to_ttm_ebitda"), "pe_ltm": mult.get("pe_ratio"),
            "ebitda_margin": prof.get("ebitda_margin"), "roic": prof.get("return_on_inv_capital"),
            "nd_ebitda": cred.get("net_debt_to_ebitda"), "period_end": mult.get("period_end_date"),
        }
        return {k: v for k, v in out.items() if v is not None}, ident
    return {}, None


def yahoo_fundamentals(ticker: str) -> dict:
    import yfinance as yf

    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as e:
        log.debug("yfinance info failed for %s: %s", ticker, e)
        return {}
    margin = info.get("ebitdaMargins")
    growth = info.get("revenueGrowth")
    out = {
        "name": info.get("shortName") or info.get("longName"),
        "currency": info.get("currency"),
        "market_cap": info.get("marketCap"), "ev": info.get("enterpriseValue"),
        "ev_sales": info.get("enterpriseToRevenue"), "ev_ebitda": info.get("enterpriseToEbitda"),
        "pe_ltm": info.get("trailingPE"), "pe_fwd": info.get("forwardPE"),
        "eps_fwd": info.get("forwardEps"),
        "rev_growth": growth * 100 if isinstance(growth, (int, float)) else None,
        "ebitda_margin": margin * 100 if isinstance(margin, (int, float)) else None,
    }
    return {k: v for k, v in out.items() if v is not None}


def get(db: DB, ticker: str, max_age_days: int = 7, refresh: bool = False) -> dict[str, Any]:
    row = db.one("SELECT fetched, data FROM fundamentals WHERE ticker=?", (ticker,))
    cached = json.loads(row["data"]) if row else {}
    if row and not refresh:
        age = datetime.utcnow() - datetime.fromisoformat(row["fetched"])
        reported = db.one("SELECT 1 FROM earnings_dates WHERE ticker=? AND date>? AND date<=?",
                          (ticker, row["fetched"][:10], datetime.utcnow().date().isoformat()))
        if age < timedelta(days=max_age_days) and not reported:
            return cached
    data: dict[str, Any] = {}
    sources: dict[str, str] = {}
    roic, ident = roic_fundamentals(ticker, cached.get("roic_id"))
    for k, v in roic.items():
        data[k] = v
        sources[k] = "ROIC.ai"
    for k, v in yahoo_fundamentals(ticker).items():
        if k not in data:
            data[k] = v
            sources[k] = "Yahoo (yfinance)"
    if ident:
        data["roic_id"] = ident
    data["sources"] = sources
    data["asof"] = datetime.utcnow().date().isoformat()
    if len(data) > 3:
        db.ex("INSERT OR REPLACE INTO fundamentals VALUES (?,?,?)",
              (ticker, datetime.utcnow().isoformat(timespec="seconds"), json.dumps(data)))
        return data
    return cached or data


def company_names(db: DB, tickers: list[str]) -> dict[str, str]:
    out = {}
    for t in tickers:
        row = db.one("SELECT data FROM fundamentals WHERE ticker=?", (t,))
        name = json.loads(row["data"]).get("name") if row else None
        out[t] = name or t
    return out
