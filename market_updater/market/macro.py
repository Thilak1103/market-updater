"""Macro data: FRED series (US rates, inflation, labour, credit spreads) and bond yields.

A series counts as a "new release" when its latest observation appeared since the last run.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from ..util import get_with_retry, log
from .prices import stooq_last_close

FRED = "https://api.stlouisfed.org/fred/series/observations"

DEFAULT_SERIES = {
    "DGS2": "UST 2y yield (%)", "DGS10": "UST 10y yield (%)", "T10YIE": "US 10y breakeven (%)",
    "BAMLH0A0HYM2": "US high-yield spread (%)", "BAMLC0A0CM": "US IG spread (%)",
    "CPIAUCSL": "US CPI index", "PAYEMS": "US payrolls (k)", "UNRATE": "US unemployment (%)",
    "RSAFS": "US retail sales ($m)", "ICSA": "US initial jobless claims",
}


@dataclass
class Obs:
    key: str
    label: str
    date: str
    value: float
    prev_date: str | None
    prev_value: float | None
    source: str

    @property
    def change(self) -> float | None:
        return None if self.prev_value is None else self.value - self.prev_value


def fred_latest(series_id: str, label: str) -> Obs | None:
    key = os.environ.get("FRED_API_KEY")
    if not key:
        return None
    r = get_with_retry(FRED, params={"series_id": series_id, "api_key": key, "file_type": "json",
                                     "sort_order": "desc", "limit": 5})
    if r.status_code != 200:
        log.warning("FRED %s -> %s", series_id, r.status_code)
        return None
    obs = [o for o in r.json().get("observations", []) if o.get("value") not in (".", None)]
    if not obs:
        return None
    cur, prev = obs[0], (obs[1] if len(obs) > 1 else None)
    return Obs(series_id, label, cur["date"], float(cur["value"]),
               prev["date"] if prev else None, float(prev["value"]) if prev else None, "FRED")


def bond_yields(symbols: dict[str, str]) -> list[Obs]:
    """Gilt and Bund 10y from Stooq (symbols set in config; checked at setup)."""
    out = []
    for label, sym in symbols.items():
        try:
            got = stooq_last_close(sym)
            if got:
                out.append(Obs(sym, label, got[0], got[1], None, None, "Stooq"))
        except Exception as e:
            log.debug("Stooq yield %s failed: %s", sym, e)
    return out


def collect(settings) -> list[Obs]:
    series = settings.get("markets.fred_series") or DEFAULT_SERIES
    out = [o for sid, label in series.items() if (o := fred_latest(sid, label))]
    out += bond_yields(settings.get("markets.stooq_yields") or {"Gilt 10y (%)": "10yuky.b",
                                                                 "Bund 10y (%)": "10ydey.b"})
    return out
