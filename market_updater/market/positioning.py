"""Positioning, street views and catalysts for the two legs of a trade idea.

All free, from yfinance (Yahoo's unofficial API), cached for the day:
- short interest (% of float, days to cover)
- analyst ratings (strong buy ... strong sell this month) and the mean price target
- EPS estimate revisions over the last 30 days (up vs down, this fiscal year)
- the next earnings date (from our own earnings calendar first)
"""
from __future__ import annotations

import json
from datetime import date

from ..db import DB
from ..util import log


def _num(v):
    try:
        f = float(v)
        return None if f != f else f     # NaN -> None
    except (TypeError, ValueError):
        return None


def fetch(ticker: str) -> dict:
    import yfinance as yf
    out: dict = {"ticker": ticker}
    t = yf.Ticker(ticker)
    try:
        info = t.info or {}
        out.update({
            "price": _num(info.get("currentPrice") or info.get("regularMarketPrice")),
            "short_pct_float": _num(info.get("shortPercentOfFloat")),
            "days_to_cover": _num(info.get("shortRatio")),
            "target_mean": _num(info.get("targetMeanPrice")),
            "n_analysts": info.get("numberOfAnalystOpinions"),
            "rating": info.get("recommendationKey"),
            "inst_owned": _num(info.get("heldPercentInstitutions")),
        })
    except Exception as e:
        log.debug("yfinance info for %s failed: %s", ticker, e)
    try:
        rs = t.recommendations_summary
        if rs is not None and len(rs):
            r0 = rs.iloc[0]
            out["ratings"] = {k: int(r0.get(k, 0) or 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")}
    except Exception as e:
        log.debug("yfinance ratings for %s failed: %s", ticker, e)
    try:
        rev = t.eps_revisions
        if rev is not None and "0y" in rev.index:
            row = rev.loc["0y"]
            out["eps_up_30d"] = int(row.get("upLast30days", 0) or 0)
            out["eps_down_30d"] = int(row.get("downLast30days", row.get("downLast30Days", 0)) or 0)
    except Exception as e:
        log.debug("yfinance revisions for %s failed: %s", ticker, e)
    try:
        tr = t.eps_trend
        if tr is not None and len(tr):
            cols = ["90daysAgo", "60daysAgo", "30daysAgo", "7daysAgo", "current"]
            out["eps_trend"] = {p: [_num(tr.loc[p].get(c)) for c in cols] for p in ("0y", "+1y") if p in tr.index}
    except Exception as e:
        log.debug("yfinance EPS trend for %s failed: %s", ticker, e)
    return out


def get(db: DB, ticker: str, today: date, fetcher=None) -> dict:
    db.ex("CREATE TABLE IF NOT EXISTS positioning (ticker TEXT, date TEXT, data TEXT, PRIMARY KEY (ticker, date))")
    row = db.one("SELECT data FROM positioning WHERE ticker=? AND date=?", (ticker, today.isoformat()))
    if row:
        return json.loads(row["data"])
    if ticker.startswith("^"):
        return {"ticker": ticker}
    try:
        data = (fetcher or fetch)(ticker)
    except Exception as e:
        log.warning("Positioning data for %s unavailable: %s", ticker, e)
        data = {"ticker": ticker}
    nxt = db.one("SELECT date FROM earnings_dates WHERE ticker=? AND date>=? ORDER BY date LIMIT 1",
                 (ticker, today.isoformat()))
    if nxt:
        data["next_earnings"] = nxt["date"]
    db.ex("INSERT OR REPLACE INTO positioning VALUES (?,?,?)", (ticker, today.isoformat(), json.dumps(data)))
    return data


def peer_value(price: float | None, own: dict, median: dict) -> tuple[float | None, str]:
    """The price if the stock re-rated to its peers' median multiple (forward P/E, else EV/EBITDA)."""
    if not price:
        return None, ""
    for key, label in (("pe_fwd", "forward P/E"), ("ev_ebitda", "EV/EBITDA")):
        o, m = own.get(key), median.get(key)
        if o and m and o > 0 and m > 0:
            return price * m / o, label
    return None, ""


def _fmt_pct(v: float | None) -> str:
    return "–" if v is None else f"{v:+.0f}%".replace("-", "−")


def _ratings(p: dict) -> str:
    r = p.get("ratings")
    if r and sum(r.values()):
        buy, hold, sell = r["strongBuy"] + r["buy"], r["hold"], r["sell"] + r["strongSell"]
        return f"{buy} buy / {hold} hold / {sell} sell"
    if p.get("rating"):
        return f"{p['rating'].replace('_', ' ')} ({p.get('n_analysts') or '?'} analysts)"
    return "–"


def table(legs: list[tuple[str, str, dict, dict, dict]], entry: tuple[float, float] | None = None,
          hedge: float | None = None) -> str:
    """The trade set-up: one column per leg. Each leg is (name, ticker, positioning, own comps, peer medians)."""
    rows: dict[str, list[str]] = {k: [] for k in (
        "Entry price", "Position", "Value at peers' multiple", "Street target", "Analysts",
        "EPS revisions, 30 days", "Short interest", "Next earnings")}
    for i, (label, ticker, p, own, med) in enumerate(legs):
        price = (entry[i] if entry and i < len(entry) else None) or p.get("price")
        rows["Entry price"].append(f"{price:,.2f}" if price else "–")
        if hedge is not None:
            rows["Position"].append("Long 1.00" if i == 0 else f"Short {hedge:.2f}")
        val, basis = peer_value(price, own, med)
        rows["Value at peers' multiple"].append(
            f"{val:,.2f} ({_fmt_pct((val / price - 1) * 100)}, {basis})" if val and price else "–")
        tgt = p.get("target_mean")
        rows["Street target"].append(f"{tgt:,.2f} ({_fmt_pct((tgt / price - 1) * 100)})" if tgt and price else "–")
        rows["Analysts"].append(_ratings(p))
        up, dn = p.get("eps_up_30d"), p.get("eps_down_30d")
        rows["EPS revisions, 30 days"].append(f"{up} up, {dn} down" if up is not None and dn is not None else "–")
        si, dtc = p.get("short_pct_float"), p.get("days_to_cover")
        rows["Short interest"].append(
            (f"{si * 100:.1f}% of float" if si is not None else "–") + (f" ({dtc:.1f} days)" if dtc and si is not None else ""))
        rows["Next earnings"].append(_short_date(p.get("next_earnings")) or "–")
    roles = ["long", "short"]
    head = "| | " + " | ".join(f"**{leg[0]}** ({roles[i] if i < 2 else 'peer'})" for i, leg in enumerate(legs)) + " |"
    lines = [head, "|---|" + "---|" * len(legs)]
    for k, vals in rows.items():
        if vals and any(v != "–" for v in vals):
            lines.append(f"| {k} | " + " | ".join(vals) + " |")
    return "\n".join(lines)


def _short_date(s: str | None) -> str:
    try:
        return date.fromisoformat((s or "")[:10]).strftime("%-d %b %Y")
    except ValueError:
        return s or ""
