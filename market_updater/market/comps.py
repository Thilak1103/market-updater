"""Comps tables for trade ideas: both legs plus peers, peer median and premium/discount."""
from __future__ import annotations

import statistics
from typing import Any, Callable

COLUMNS = [
    ("market_cap", "Mkt cap", "money"), ("ev_ebitda", "EV/EBITDA", "x"), ("ev_sales", "EV/Sales", "x"),
    ("pe_ltm", "P/E LTM", "x"), ("pe_fwd", "P/E fwd*", "x"), ("rev_growth", "Rev growth", "pct"),
    ("ebitda_margin", "EBITDA mgn", "pct"), ("roic", "ROIC", "pct"), ("nd_ebitda", "ND/EBITDA", "x"),
    ("ret_1m", "1m", "pct"), ("ret_ytd", "YTD", "pct"),
]


def _fmt(v: Any, kind: str) -> str:
    if v is None:
        return "–"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "–"
    if kind == "money":
        for unit, div in (("tn", 1e12), ("bn", 1e9), ("m", 1e6)):
            if abs(v) >= div:
                return f"{v / div:.1f}{unit}"
        return f"{v:.0f}"
    if kind == "pct":
        return f"{v:+.1f}%" if abs(v) < 1000 else "–"
    return f"{v:.1f}x" if 0 < v < 500 else "–"


def build(legs: list[str], peers: list[str], fundamentals: Callable[[str], dict],
          returns: Callable[[str, str], float | None]) -> dict:
    """Return {'rows': [...], 'median': {...}, 'premium': {...}, 'markdown': str}."""
    tickers = list(dict.fromkeys(legs + [p for p in peers if p not in legs]))
    rows = []
    for t in tickers:
        f = dict(fundamentals(t) or {})
        f["ret_1m"] = returns(t, "1m")
        f["ret_ytd"] = returns(t, "ytd")
        f["ticker"] = t
        rows.append(f)
    peer_rows = [r for r in rows if r["ticker"] not in legs] or rows
    median = {}
    for key, _, _ in COLUMNS:
        vals = [float(r[key]) for r in peer_rows if isinstance(r.get(key), (int, float))]
        median[key] = statistics.median(vals) if vals else None
    premium = {}
    for leg in legs:
        r = next(x for x in rows if x["ticker"] == leg)
        premium[leg] = {}
        for key in ("ev_ebitda", "pe_fwd", "pe_ltm"):
            if isinstance(r.get(key), (int, float)) and median.get(key):
                premium[leg][key] = (float(r[key]) / median[key] - 1) * 100
    head = "| Company | " + " | ".join(c[1] for c in COLUMNS) + " |"
    sep = "|" + "---|" * (len(COLUMNS) + 1)
    lines = [head, sep]
    for r in rows:
        name = r.get("name")
        label = f"{name} ({r['ticker']})" if name and name != r["ticker"] else r["ticker"]
        if r["ticker"] in legs:
            label = f"**{label}**"
        lines.append("| " + label + " | " + " | ".join(_fmt(r.get(k), kind) for k, _, kind in COLUMNS) + " |")
    lines.append("| Peer median | " + " | ".join(_fmt(median.get(k), kind) for k, _, kind in COLUMNS) + " |")
    srcs = sorted({s for r in rows for s in (r.get("sources") or {}).values()} | {"Yahoo/Stooq prices"})
    asof = max((r.get("asof") or "" for r in rows), default="")
    note = (f"Sources: {', '.join(srcs)}; fundamentals as of {asof or 'latest available'}. "
            "*Forward P/E uses Yahoo's consensus via yfinance (unofficial).")
    return {"rows": rows, "median": median, "premium": premium,
            "markdown": "\n".join(lines) + "\n\n" + note}


def premium_sentence(premium: dict[str, dict[str, float]]) -> str:
    parts = []
    for leg, p in premium.items():
        bits = [f"{'EV/EBITDA' if k == 'ev_ebitda' else 'forward P/E' if k == 'pe_fwd' else 'LTM P/E'} "
                f"{v:+.0f}% vs peers" for k, v in p.items() if k in ("ev_ebitda", "pe_fwd")]
        if bits:
            parts.append(f"{leg}: " + ", ".join(bits))
    return "; ".join(parts)
