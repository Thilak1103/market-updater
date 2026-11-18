"""Pair statistics for trade ideas: hedge ratio, correlation, spread position, stop and P&L.

Convention: long A, short beta x B (dollar terms), where beta is the slope of A's daily log
returns on B's over the last year. The spread is ln(A) - beta * ln(B).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd


@dataclass
class PairStats:
    long: str
    short: str
    beta: float
    corr: float
    n_days: int
    spread_now: float
    spread_mean_60: float
    spread_std_60: float
    zscore_60: float
    stop_spread: float          # spread level that triggers the stop (long-spread position)
    entry_long: float
    entry_short: float

    def as_dict(self) -> dict:
        return asdict(self)


def pair_stats(prices: pd.DataFrame, long: str, short: str, lookback: int = 252,
               z_window: int = 60, stop_mult: float = 1.5) -> PairStats | None:
    if long not in prices.columns or short not in prices.columns:
        return None
    df = prices[[long, short]].dropna()
    if len(df) < max(z_window + 5, 80):
        return None
    df = df.iloc[-(lookback + 1):]
    la, lb = np.log(df[long]), np.log(df[short])
    ra, rb = la.diff().dropna(), lb.diff().dropna()
    var_b = float(rb.var())
    if var_b == 0:
        return None
    beta = float(((ra - ra.mean()) * (rb - rb.mean())).sum() / ((len(rb) - 1) * var_b))
    corr = float(ra.corr(rb))
    spread = la - beta * lb
    recent = spread.iloc[-z_window:]
    mean, std = float(recent.mean()), float(recent.std())
    now = float(spread.iloc[-1])
    z = (now - mean) / std if std else 0.0
    return PairStats(long, short, round(beta, 3), round(corr, 3), len(ra), now, mean, std,
                     round(z, 2), now - stop_mult * std,
                     float(df[long].iloc[-1]), float(df[short].iloc[-1]))


def pair_pnl(entry_long: float, entry_short: float, beta: float, last_long: float,
             last_short: float) -> float:
    """Return on the long leg's notional: long leg return minus beta x short leg return."""
    return (last_long / entry_long - 1) - beta * (last_short / entry_short - 1)


def stopped(stats_beta: float, stop_spread: float, last_long: float, last_short: float) -> bool:
    return (np.log(last_long) - stats_beta * np.log(last_short)) <= stop_spread
