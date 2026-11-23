"""Long-term memory: what the briefing learns from everything it reads.

1. Calls: every time a named writer, analyst or newsletter is bullish or bearish on one stock
   we track, that is recorded with the price that day. After 20 trading days the call is
   marked right or wrong against the stock's sector ETF.
2. Track record: each voice's hit rate and average excess return, once it has enough calls.
3. What the newsletters keep pitching: per stock, who has been bullish or bearish in the last
   30 days.
4. What Thilak acts on: which ideas he followed or built out, and how the system's ideas have
   done by origin.

The trade-idea step reads all four, so ideas lean on voices that have been right, notice
names many writers are circling, and learn from past outcomes.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from ..db import DB
from ..market.prices import Prices
from ..settings import Settings
from ..util import log

HORIZON = 20          # trading days before a call is judged
MIN_CALLS = 5         # calls needed before a voice's record is shown

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    claim_id INTEGER PRIMARY KEY, voice TEXT, source TEXT, ticker TEXT, direction INTEGER,
    date TEXT, entry REAL, bench TEXT, bench_entry REAL, claim TEXT,
    resolved INTEGER DEFAULT 0, excess REAL, hit INTEGER
);
CREATE INDEX IF NOT EXISTS ix_calls_voice ON calls(voice);
CREATE INDEX IF NOT EXISTS ix_calls_ticker ON calls(ticker, date);
"""


def _ensure(db: DB) -> None:
    db.conn.executescript(SCHEMA)


def _bench(settings: Settings, ticker: str) -> str:
    for s in settings.sectors:
        if ticker in s.watchlist and s.proxies:
            return s.proxies[0]
    return "^GSPC"


def record_calls(settings: Settings, db: DB, claims: list[dict], prices: Prices, today: date) -> int:
    """Store today's single-stock bullish or bearish views with entry prices."""
    _ensure(db)
    frame = prices.frame
    n = 0
    for c in claims:
        tickers = c.get("tickers") or []
        if c.get("stance") not in ("bull", "bear") or len(tickers) != 1 or c.get("kind") not in (
                "opinion", "forecast", "position"):
            continue
        t = tickers[0]
        bench = _bench(settings, t)
        if t not in frame.columns or bench not in frame.columns:
            continue
        q, b = prices.quote(t), prices.quote(bench)
        if not q or not b:
            continue
        voice = (c.get("speaker") or c.get("source_name") or "").strip()
        if not voice:
            continue
        before = db.conn.total_changes
        db.ex("INSERT OR IGNORE INTO calls(claim_id, voice, source, ticker, direction, date, entry, bench, "
                   "bench_entry, claim) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (c["id"], voice, c.get("source_name"), t, 1 if c["stance"] == "bull" else -1,
                    today.isoformat(), q.last, bench, b.last, (c.get("claim") or "")[:300]))
        n += db.conn.total_changes - before
    return n


def score_calls(db: DB, prices: Prices) -> int:
    """Judge calls that are at least HORIZON trading days old."""
    _ensure(db)
    frame = prices.frame
    if frame.empty:
        return 0
    done = 0
    for r in db.q("SELECT * FROM calls WHERE resolved=0"):
        if r["ticker"] not in frame.columns or r["bench"] not in frame.columns:
            continue
        after = frame.loc[frame.index > r["date"], [r["ticker"], r["bench"]]].dropna()
        if len(after) < HORIZON:
            continue
        px, bx = after.iloc[HORIZON - 1]
        excess = (px / r["entry"] - 1) - (bx / r["bench_entry"] - 1)
        excess *= r["direction"]
        db.ex("UPDATE calls SET resolved=1, excess=?, hit=? WHERE claim_id=?",
              (float(excess), int(excess > 0), r["claim_id"]))
        done += 1
    if done:
        log.info("Judged %d writer calls after %d trading days", done, HORIZON)
    return done


def track_record(db: DB, min_calls: int = MIN_CALLS) -> list[dict]:
    _ensure(db)
    rows = db.q("SELECT voice, COUNT(*) n, AVG(hit) hit, AVG(excess) ex FROM calls WHERE resolved=1 "
                "GROUP BY voice HAVING COUNT(*)>=? ORDER BY AVG(hit) DESC, AVG(excess) DESC", (min_calls,))
    return [{"voice": r["voice"], "n": r["n"], "hit": r["hit"], "excess": r["ex"]} for r in rows]


def pitched_lately(db: DB, today: date, days: int = 30, top: int = 15) -> list[dict]:
    """Stocks the most voices have been bullish or bearish on lately."""
    _ensure(db)
    since = (today - timedelta(days=days)).isoformat()
    rows = db.q("SELECT ticker, direction, voice, MAX(date) d FROM calls WHERE date>=? "
                "GROUP BY ticker, direction, voice", (since,))
    agg: dict[str, dict] = {}
    for r in rows:
        a = agg.setdefault(r["ticker"], {"ticker": r["ticker"], "bulls": [], "bears": [], "last": r["d"]})
        (a["bulls"] if r["direction"] > 0 else a["bears"]).append(r["voice"])
        a["last"] = max(a["last"], r["d"])
    out = sorted(agg.values(), key=lambda a: -(len(a["bulls"]) + len(a["bears"])))
    return out[:top]


def preferences(db: DB) -> dict:
    """What Thilak acts on, and how the system's own ideas have done by origin."""
    rows = [dict(r) for r in db.q("SELECT * FROM ideas")]
    if not rows:
        return {}
    followed = [r for r in rows if r.get("following")]
    built = [r for r in rows if r.get("build_out")]
    by_origin: dict[str, list[float]] = {}
    for r in rows:
        if r["status"] != "Open":
            by_origin.setdefault(r.get("origin") or "Unknown", []).append(r.get("pnl") or 0.0)
    return {
        "followed": [f"{r['long']}/{r['short']} ({r.get('origin')}, {r.get('sector')})" for r in followed[-10:]],
        "built_out": [f"{r['long']}/{r['short']}" for r in built[-10:]],
        "outcomes_by_origin": {k: {"closed": len(v), "made_money": sum(x > 0 for x in v),
                                   "avg_return": round(sum(v) / len(v), 4)} for k, v in by_origin.items()},
    }


def context(db: DB, today: date) -> str:
    """The memory block handed to the trade-idea step."""
    lines = []
    tr = track_record(db)
    if tr:
        lines.append("Track record of voices (calls judged after 20 trading days against the sector ETF; "
                     "lean on the ones that have been right, discount the ones that have not):")
        lines += [f"- {r['voice']}: {r['hit'] * 100:.0f}% right over {r['n']} calls, average "
                  f"{r['excess'] * 100:+.1f}% vs sector" for r in tr[:20]]
    pl = pitched_lately(db, today)
    if pl:
        lines.append("\nWhat your newsletters and voices have been pitching in the last 30 days (by number of voices):")
        lines += [f"- {a['ticker']}: bullish {', '.join(a['bulls'][:6]) or 'none'}; bearish "
                  f"{', '.join(a['bears'][:6]) or 'none'}" for a in pl]
    pref = preferences(db)
    if pref:
        lines.append("\nWhat Thilak acts on and how past ideas did: " + json.dumps(pref, ensure_ascii=False))
    return "\n".join(lines)


def scorecard_markdown(db: DB) -> str:
    """Friday: who has been right."""
    tr = track_record(db)
    if not tr:
        n = db.one("SELECT COUNT(*) n FROM calls")["n"] if db.one("SELECT name FROM sqlite_master WHERE name='calls'") else 0
        return (f"*Tracking {n} calls from your sources so far. A voice appears here once {MIN_CALLS} of its calls "
                f"have been judged after {HORIZON} trading days.*")
    lines = ["| Voice | Calls judged | Right | Average vs sector |", "|---|---|---|---|"]
    for r in tr[:10]:
        ex = f"{r['excess'] * 100:+.1f}%".replace("-", "−")
        lines.append(f"| {r['voice']} | {r['n']} | {r['hit'] * 100:.0f}% | {ex} |")
    return "\n".join(lines)
