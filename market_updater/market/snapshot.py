"""The morning market snapshot: overview tables, sector moves and "Elsewhere" triggers.

Everything here is computed in code. The writing step receives these tables as fixed text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from ..db import DB
from ..settings import Settings
from .macro import Obs
from .prices import Prices, Quote


@dataclass
class Trigger:
    sector: str
    reason: str
    magnitude: float


@dataclass
class Snapshot:
    asof_label: str
    overview_md: str
    sector_tables: dict[str, str]              # sector key -> markdown table of moves
    macro_md: str
    triggers: list[Trigger] = field(default_factory=list)
    numbers: set[str] = field(default_factory=set)   # every number printed, for checks
    quotes: dict[str, Quote] = field(default_factory=dict)
    macro: list[Obs] = field(default_factory=list)
    sector_rows: dict[str, list[dict]] = field(default_factory=dict)  # sector -> [{ticker, chg, r1m, ytd}]
    key_rows: list[dict] = field(default_factory=list)                 # "The numbers that matter"


def _pct(v: float | None) -> str:
    return "–" if v is None else f"{v:+.2f}%"


def _lvl(v: float) -> str:
    return f"{v:,.2f}" if abs(v) < 100000 else f"{v:,.0f}"


def _row(name: str, q: Quote | None) -> str:
    if not q:
        return f"| {name} | – | – | – |"
    flag = " ⚠" if q.cross_checked is False else ""
    return f"| {name} | {_lvl(q.last)}{flag} | {_pct(q.change_pct)} | {q.asof} |"


def build(settings: Settings, prices: Prices, macro: list[Obs], db: DB, day: date,
          asof_label: str, earnings_surprises: list[tuple[str, float]] | None = None) -> Snapshot:
    numbers: set[str] = set()
    quotes: dict[str, Quote] = {}

    def q(t: str) -> Quote | None:
        if t not in quotes:
            got = prices.quote(t)
            if got:
                quotes[t] = got
        res = quotes.get(t)
        if res:
            numbers.update({_lvl(res.last), f"{res.change_pct:+.2f}", f"{abs(res.change_pct):.2f}",
                            f"{abs(res.change_pct):.1f}"})
        return res

    mk = settings.get("markets", {}) or {}
    lines = ["| Market | Level | Change | As of |", "|---|---|---|---|"]
    for group in ("indices", "commodities"):
        for m in mk.get(group, []):
            if m.get("ticker"):
                lines.append(_row(m["name"], q(m["ticker"])))
    overview = "\n".join(lines)

    mlines = ["| Series | Latest | Previous | Date | Source |", "|---|---|---|---|---|"]
    for o in macro:
        prev = "–" if o.prev_value is None else f"{o.prev_value:,.2f}"
        mlines.append(f"| {o.label} | {o.value:,.2f} | {prev} | {o.date} | {o.source} |")
        numbers.update({f"{o.value:,.2f}", f"{o.value:.2f}", f"{o.value:.1f}"})
    macro_md = "\n".join(mlines)

    triggers: list[Trigger] = []
    thr_sector = float(settings.get("thresholds.elsewhere_sector_move_pct", 2.0))
    thr_stock = float(settings.get("thresholds.elsewhere_single_stock_move_pct", 7.0))
    tables: dict[str, str] = {}
    sector_rows: dict[str, list[dict]] = {}
    for sec in settings.sectors:
        if sec.coverage == "off":
            continue
        rows = ["| Name | Last | Day | 1m | YTD |", "|---|---|---|---|---|"]
        srows: list[dict] = []
        movers = [t for t in sec.watchlist if q(t)] if sec.core else []
        movers.sort(key=lambda t: abs(quotes[t].change_pct), reverse=True)
        for t in sec.proxies + movers[:6]:
            qt = q(t)
            if not qt:
                continue
            r1m, ytd = prices.returns(t, 21), prices.ytd(t)
            for v in (r1m, ytd):
                if v is not None:
                    numbers.update({f"{v:+.1f}", f"{abs(v):.1f}"})
            srows.append({"ticker": t, "proxy": t in sec.proxies, "chg": qt.change_pct, "r1m": r1m, "ytd": ytd})
            rows.append(f"| {t} | {_lvl(qt.last)} | {_pct(qt.change_pct)} | "
                        f"{'–' if r1m is None else f'{r1m:+.1f}%'} | {'–' if ytd is None else f'{ytd:+.1f}%'} |")
            if not sec.core:
                if t in sec.proxies and abs(qt.change_pct) >= thr_sector:
                    triggers.append(Trigger(sec.key, f"{t} {qt.change_pct:+.1f}%", abs(qt.change_pct)))
            elif t in sec.watchlist and abs(qt.change_pct) >= thr_stock:
                triggers.append(Trigger(sec.key, f"{t} {qt.change_pct:+.1f}% single-stock move",
                                        abs(qt.change_pct)))
        tables[sec.key] = "\n".join(rows)
        sector_rows[sec.key] = srows

    thr_surprise = float(settings.get("thresholds.earnings_surprise_pct", 5.0))
    for ticker, surprise in earnings_surprises or []:
        if abs(surprise) >= thr_surprise:
            sec = settings.sector_of_ticker(ticker) or "elsewhere"
            triggers.append(Trigger(sec, f"{ticker} EPS {surprise:+.0f}% vs consensus", abs(surprise)))

    with db.tx() as c:
        c.executemany("INSERT INTO elsewhere_log VALUES (?,?,?,?)",
                      [(day.isoformat(), t.sector, t.reason, t.magnitude) for t in triggers
                       if not (settings.sector(t.sector) and settings.sector(t.sector).core)])
    keys = key_rows(settings, quotes, q, macro, day, numbers)
    return Snapshot(asof_label, overview, tables, macro_md, triggers, numbers, quotes, list(macro),
                    sector_rows, keys)


# Rows of "The numbers that matter". Always shown: the anchor markets; other indices only when
# they moved at least 1%. Rates and spreads always; data releases only in the week they come out.
ANCHORS = {"S&P 500", "Nasdaq 100", "FTSE 100", "Euro Stoxx 50", "Nikkei 225", "VIX", "Brent", "Gold"}
RATES = ("DGS2", "DGS10", "BAMLH0A0HYM2", "BAMLC0A0CM", "T10YIE")
RELEASES = ("CPIAUCSL", "PAYEMS", "UNRATE", "RSAFS", "ICSA")
# For these a rise is bad news for risk (shown red); for everything else a rise is green.
UP_IS_BAD = {"VIX", "BAMLH0A0HYM2", "BAMLC0A0CM", "DGS2", "DGS10", "UNRATE", "ICSA"}


def _arrow(v: float, text: str, up_is_bad: bool = False) -> str:
    if abs(v) < 1e-9:
        return f"▬ {text}"
    up = v > 0
    color = ("red" if up else "green") if up_is_bad else ("green" if up else "red")
    return f'<span color="{color}">{"▲" if up else "▼"} {text}</span>'


def key_rows(settings: Settings, quotes: dict, q, macro: list[Obs], day: date, numbers: set[str]) -> list[dict]:
    mk = settings.get("markets", {}) or {}
    rows: list[dict] = []
    for group in ("indices", "commodities"):
        for m in mk.get(group, []):
            t = m.get("ticker")
            qt = q(t) if t else None
            if not qt:
                continue
            if m["name"] not in ANCHORS and abs(qt.change_pct) < 1.0:
                continue
            level = _lvl(qt.last)
            if group == "commodities" and m["name"] in ("Brent", "WTI", "Gold"):
                level = "$" + level
            rows.append({"name": m["name"], "level": level,
                         "move": _arrow(qt.change_pct, f"{abs(qt.change_pct):.1f}%", m["name"] in UP_IS_BAD),
                         "raw": f"{qt.change_pct:+.2f}%"})
    for o in macro:
        is_rate = o.key in RATES or o.source == "Stooq"
        recent = False
        try:
            recent = (day - date.fromisoformat(o.date[:10])).days <= 7
        except ValueError:
            pass
        if not is_rate and not (o.key in RELEASES and recent):
            continue
        name = o.label.replace(" (%)", "")
        if is_rate or "(%)" in o.label:
            level = f"{o.value:.2f}%"
            if o.prev_value is not None:
                bp = round((o.value - o.prev_value) * 100)
                numbers.update({f"{abs(bp)}bp", str(abs(bp))})
                move = _arrow(bp, f"{abs(bp)}bp", o.key in UP_IS_BAD or o.source == "Stooq")
            else:
                move = "–"
        else:
            level = f"{o.value:,.0f}" if o.value > 1000 else f"{o.value:,.2f}"
            move = "–" if o.prev_value is None else _arrow(o.value - o.prev_value,
                                                           f"from {o.prev_value:,.0f}" if o.prev_value > 1000
                                                           else f"from {o.prev_value:,.2f}", o.key in UP_IS_BAD)
            name = f"{name} ({o.date[:10]})"
        numbers.add(level.rstrip("%"))
        rows.append({"name": name, "level": level, "move": move, "raw": level})
    return rows
