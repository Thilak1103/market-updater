"""Trade ideas: a statistical shortlist from code, the pitch from Claude Opus, comps, targets,
charts and day-by-day tracking in SQLite and Notion.

Every idea has a target, a stop and a horizon, set in code from the pair's spread:
- spread = ln(long) - beta x ln(short), beta from a year of daily returns;
- stop: the spread falls a further `stop_spread_vol_multiple` (1.5) x its 60-day volatility;
- target: back to its 60-day average if it sits well below it, otherwise up by
  `target_spread_vol_multiple` (2) x its 60-day volatility;
- horizon: `max_holding_trading_days` (20) trading days.
"""
from __future__ import annotations

import io
import itertools
import json
import math
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

from ..analysis.views import DebateState
from ..db import DB
from ..llm import LLMUnavailable
from ..market import comps as C
from ..market import fundamentals as F
from ..market import positioning as POS
from ..market.pairs import PairStats, pair_pnl, pair_stats, stopped
from ..market.prices import Prices
from ..market.snapshot import Snapshot
from ..settings import Settings
from ..util import log
from . import prompts as P
from ..analysis.checks import CITE
from .sections import claim_line, clean, select

CONVICTION_COLOR = {"High": "green", "Medium": "orange", "Low": "gray"}


@dataclass
class Idea:
    spec: dict
    stats: PairStats
    comps: dict
    markdown: str = ""
    problems: list[str] = field(default_factory=list)
    cited: set[int] = field(default_factory=set)
    target_spread: float = 0.0
    target_return: float = 0.0
    stop_return: float = 0.0
    horizon_end: str = ""
    target_label: str = ""
    chart: bytes | None = None
    texts: dict = field(default_factory=dict)
    pitch: str = ""            # the markdown before the briefing's chart is put in
    score: int = 0             # the reviewer's 1-10 score
    critique: str = ""


def universe(settings: Settings) -> dict[str, str]:
    out = {}
    for s in settings.sectors:
        if s.coverage == "off":
            continue
        for t in s.watchlist + s.proxies:
            out.setdefault(t, s.key)
    return out


def open_ideas(db: DB) -> list[dict]:
    return [dict(r) for r in db.q("SELECT * FROM ideas WHERE status='Open'")]


# -- targets and tracking ---------------------------------------------------------

def plan(st: PairStats, cfg: dict, today: date) -> tuple[float, float, float, str, str]:
    """(target spread, target return, stop return, horizon end ISO, target label)."""
    t_mult = float(cfg.get("target_spread_vol_multiple", 2.0))
    s_mult = float(cfg.get("stop_spread_vol_multiple", 1.5))
    days = int(cfg.get("max_holding_trading_days", 20))
    std = st.spread_std_60 or 1e-6
    gap = st.spread_mean_60 - st.spread_now
    if st.zscore_60 <= -0.75 and 0 < gap <= t_mult * std:
        target = st.spread_mean_60
        label = "the spread back to its 60-day average"
    elif st.zscore_60 <= -0.75:
        target = st.spread_now + t_mult * std     # don't promise the whole gap back in a month
        label = f"the spread recovers {t_mult:g}× its 60-day volatility, part of the way to its 60-day average"
    else:
        target = st.spread_now + t_mult * std
        label = f"the spread moves {t_mult:g}× its 60-day volatility our way"
    target_ret = math.exp(target - st.spread_now) - 1
    stop_ret = math.exp(-s_mult * std) - 1
    end = np.busday_offset(np.datetime64(today.isoformat()), days, roll="forward")
    return target, target_ret, stop_ret, str(end), label


def update_open(db: DB, prices: Prices, settings: Settings, today: date) -> list[dict]:
    """Mark open ideas to market; close on target, stop or expiry. Returns one dict per idea."""
    cfg = settings.get("trade_ideas", {}) or {}
    max_days = int(cfg.get("max_holding_trading_days", 20))
    t_mult = float(cfg.get("target_spread_vol_multiple", 2.0))
    out = []
    for idea in open_ideas(db):
        ql, qs = prices.quote(idea["long"]), prices.quote(idea["short"])
        if not ql or not qs:
            continue
        beta = idea["hedge_ratio"]
        pnl = pair_pnl(idea["entry_long"], idea["entry_short"], beta, ql.last, qs.last)
        target_spread = idea.get("target_spread")
        target_ret = idea.get("target_return")
        if target_spread is None:          # ideas opened before targets existed
            target_spread = idea["entry_spread"] + t_mult * (idea["spread_std"] or 0)
            target_ret = math.exp(target_spread - idea["entry_spread"]) - 1
        spread = math.log(ql.last) - beta * math.log(qs.last)
        held = len(prices.frame.loc[prices.frame.index > idea["created"][:10]]) if not prices.frame.empty else 0
        best = max(idea.get("best") or 0.0, pnl)
        worst = min(idea.get("worst") or 0.0, pnl)
        status, outcome, reason = "Open", "Open", None
        if spread >= target_spread:
            status, outcome, reason = "Closed", "Hit target", "the spread reached its target"
        elif stopped(beta, idea["stop_spread"], ql.last, qs.last):
            status, outcome, reason = "Stopped out", "Stopped out", "the spread hit the stop"
        elif held >= max_days:
            status, outcome = "Expired", "Expired up" if pnl > 0 else "Expired down"
            reason = f"{max_days} trading days elapsed"
        db.ex("UPDATE ideas SET pnl=?, status=?, outcome=?, closed=?, close_reason=?, best=?, worst=?, "
              "target_spread=?, target_return=? WHERE id=?",
              (pnl, status, outcome, today.isoformat() if status != "Open" else None, reason, best, worst,
               target_spread, target_ret, idea["id"]))
        out.append({"id": idea["id"], "long": idea["long"], "short": idea["short"], "pnl": pnl, "status": status,
                    "outcome": outcome, "reason": reason, "day": min(held, max_days), "max_days": max_days,
                    "progress": (pnl / target_ret) if target_ret else None, "following": bool(idea.get("following")),
                    "headline": idea.get("headline")})
    return out


STATUS_COLOR = {"On track": "green", "Watch": "orange", "Needs a tweak": "yellow", "Broken": "red"}


def thesis_check(llm, db: DB, settings: Settings, claims: list[dict], snap: Snapshot,
                 updates: list[dict], today: date) -> list[dict]:
    """Ask, for every open idea: is the thesis playing out? Saves the answer and returns the rows."""
    live = {u["id"]: u for u in updates if u["status"] == "Open"}
    if not live:
        return []
    ideas = {r["id"]: dict(r) for r in db.q("SELECT * FROM ideas WHERE status='Open'") if r["id"] in live}
    blocks, by_id = [], {}
    for iid, r in ideas.items():
        u = live[iid]
        spec = json.loads(r.get("spec") or "{}")
        legs = {r["long"], r["short"]}
        news = [c for c in claims if legs & set(c.get("tickers") or [])][:8]
        by_id.update({c["id"]: c for c in news})
        prog = f"{u['progress'] * 100:.0f}% of the way to target" if u.get("progress") is not None else "n/a"
        blocks.append("\n".join([
            f"Idea {iid}: long {r['long']} / short {r['short']}: {r.get('headline') or ''}",
            f"Thesis: {r.get('thesis') or ''}",
            f"Proves it wrong: {r.get('wrong') or ''}",
            f"Catalysts: {spec.get('catalysts') or spec.get('catalyst') or 'none given'}",
            f"So far: day {u['day']} of {u['max_days']}, return {u['pnl'] * 100:+.1f}% hedged, {prog}, "
            f"target {(r.get('target_return') or 0) * 100:+.1f}%, stop {(r.get('stop_return') or 0) * 100:+.1f}%",
            "New material:", *(claim_line(c, settings.people) for c in news)]) if news else
            "\n".join([f"Idea {iid}: long {r['long']} / short {r['short']}: {r.get('headline') or ''}",
                       f"Thesis: {r.get('thesis') or ''}", f"Proves it wrong: {r.get('wrong') or ''}",
                       f"So far: day {u['day']} of {u['max_days']}, return {u['pnl'] * 100:+.1f}% hedged, {prog}",
                       "New material: none today"]))
    try:
        res = llm.json(tier="write", purpose="thesis_check", system=P.THESIS_CHECK, user="\n\n".join(blocks),
                       schema=P.THESIS_SCHEMA, max_tokens=2500) or {}
    except (LLMUnavailable, ValueError) as e:
        log.warning("Thesis check failed: %s", e)
        return []
    out = []
    for row in res.get("rows", []):
        iid = row.get("id")
        if iid not in ideas:
            continue
        note, _, _ = clean(row.get("note", ""), by_id, snap)
        tweak, _, _ = clean(row.get("tweak", "") or "", by_id, snap)
        status = row.get("status") if row.get("status") in STATUS_COLOR else "Watch"
        full = note + (f" **Tweak:** {tweak}" if tweak else "")
        db.ex("UPDATE ideas SET thesis_status=?, thesis_note=?, thesis_checked=? WHERE id=?",
              (status, full, today.isoformat(), iid))
        out.append({**live[iid], "thesis_status": status, "thesis_note": full})
    return out


def book_markdown(checks: list[dict], updates: list[dict], dashboard_id: str | None) -> str:
    """Table of open ideas: day, return, and whether the thesis is playing out. Followed ideas first (⭐)."""
    rows = {c["id"]: c for c in checks}
    live = [rows.get(u["id"], u) for u in updates]
    if not live:
        return ""
    live.sort(key=lambda u: (not u["following"], u["status"] != "Open", -(u.get("day") or 0)))
    lines = ["| Idea | Day | Return | Thesis | What changed |", "|---|---|---|---|---|"]
    for u in live:
        pnl = f"{u['pnl'] * 100:+.1f}%".replace("-", "−")
        col = "green" if u["pnl"] > 0 else "red" if u["pnl"] < 0 else "gray"
        if u["status"] != "Open":
            thesis, note, day = f"Closed: {u['outcome'].lower()}", u.get("reason") or "", "–"
        else:
            st = u.get("thesis_status") or "–"
            thesis = f'<span color="{STATUS_COLOR.get(st, "gray")}">{st}</span>' if st != "–" else "–"
            note, day = u.get("thesis_note") or "", f"{u['day']} of {u['max_days']}"
        star = "⭐ " if u["following"] else ""
        lines.append(f"| {star}Long {u['long']} / short {u['short']} | {day} | <span color=\"{col}\">{pnl}</span> | "
                     f"{thesis} | {note} |")
    link = f"\n*⭐ = ideas you follow. Full tracker: [Trade idea dashboard](notion:{dashboard_id}).*" if dashboard_id else ""
    return "\n".join(lines) + link


def following_markdown(updates: list[dict], dashboard_id: str | None) -> str:
    """The yellow 'Ideas you're following' callout (empty string if nothing is followed)."""
    mine = [u for u in updates if u["following"]]
    if not mine:
        return ""
    bits = []
    for u in mine:
        pnl = f"{u['pnl'] * 100:+.1f}%".replace("-", "−")
        col = "green" if u["pnl"] > 0 else "red" if u["pnl"] < 0 else "gray"
        state = (f"day {u['day']} of {u['max_days']}" if u["status"] == "Open"
                 else f"closed: {u['outcome'].lower()}")
        prog = f", {max(0, u['progress']) * 100:.0f}% of the way to target" if u["progress"] and u["status"] == "Open" else ""
        bits.append(f"Long {u['long']} / short {u['short']} <span color=\"{col}\">{pnl}</span> ({state}{prog})")
    link = f" Full tracker: [Trade idea dashboard](notion:{dashboard_id})." if dashboard_id else ""
    return "\n".join(["::: callout ⭐ yellow", "**Ideas you're following:** " + " · ".join(bits) + "." + link, ":::"])


# -- the shortlist -----------------------------------------------------------------

def shortlist(settings: Settings, prices: Prices, uni: dict[str, str], min_corr: float,
              exclude: set[str], limit: int = 45) -> list[PairStats]:
    """Well-correlated pairs within each sector (watch-list names and the sector's ETFs)."""
    out: list[PairStats] = []
    frame = prices.frame
    by_sector: dict[str, list[str]] = {}
    for t, s in uni.items():
        if t in frame.columns and not t.startswith("^") and t not in exclude:
            by_sector.setdefault(s, []).append(t)
    for sec, tickers in by_sector.items():
        for a, b in itertools.combinations(sorted(tickers), 2):
            st = pair_stats(frame, a, b)
            if st and st.corr >= min_corr:
                out.append(st)
    # Most interesting first: stretched spreads, then tight relationships.
    out.sort(key=lambda s: (-abs(s.zscore_60), -s.corr))
    return out[:limit]


def _context(settings: Settings, states: list[DebateState], claims: list[dict], reports: list,
             snap: Snapshot, db: DB | None = None, today: date | None = None) -> tuple[str, dict[int, dict]]:
    people = settings.people
    idea_sources = {s.name.lower() for s in settings.sources if s.ideas or s.contrarian}
    pitches = [c for c in claims if (c.get("source_name") or "").lower() in idea_sources and c.get("tickers")]
    chosen = select(claims, people, limit=90)
    chosen += [c for c in pitches if c not in chosen][:30]
    blocks = []
    for s in states[:8]:
        blocks.append(f"Debate {s.key} ({s.question}): majority {s.majority} {int(s.share * 100)}%, "
                      f"crowded={s.crowded}, cracking={s.cracking}, dissenters="
                      f"{[d['speaker'] for d in s.dissenters]}, changed={[d['speaker'] for d in s.changed]}")
    rep_lines = [f"{r.ticker}: move {r.move_pct}, sector {r.sector_move_pct}, usual abs move "
                 f"{r.avg_abs_move_pct}, reaction_fit={getattr(r, 'reaction_fit', None)}" for r in reports]
    trig = [f"{t.sector}: {t.reason}" for t in snap.triggers]
    text = ("Debates:\n" + "\n".join(blocks) + "\n\nEarnings reactions:\n" + "\n".join(rep_lines)
            + "\n\nSector and stock triggers:\n" + "\n".join(trig)
            + "\n\nMaterial:\n" + "\n".join(claim_line(c, people) for c in chosen))
    if db is not None:
        from ..analysis import memory as MEM
        mem = MEM.context(db, today or date.today())
        if mem:
            text += "\n\nWhat the briefing has learned so far:\n" + mem
    return text, {c["id"]: c for c in chosen}


def _ask(llm, system: str, user: str) -> list[dict]:
    try:
        res = llm.json(tier="trade_ideas", purpose="trade_ideas", system=system, user=user,
                       schema=P.IDEAS_SCHEMA, max_tokens=8000)
        return res.get("ideas", []) or []
    except (LLMUnavailable, ValueError) as e:
        log.warning("Trade-idea call failed: %s", e)
        return []


def recently_pitched(db: DB, today: date, days: int) -> set[str]:
    """Every ticker used in an idea opened in the last `days` calendar days, or still open."""
    since = (today - timedelta(days=days)).isoformat()
    rows = db.q("SELECT long, short FROM ideas WHERE created>=? OR status='Open'", (since,))
    return {t for r in rows for t in (r["long"], r["short"])}


def generate(settings: Settings, db: DB, llm, prices: Prices, states: list[DebateState],
             claims: list[dict], reports: list, snap: Snapshot, today: date | None = None) -> list[Idea]:
    """Pitch only ideas that survive review. Up to three rounds of: propose -> statistics and
    evidence checks -> comps and positioning -> sceptical review (score 1-10). Ideas scoring
    below `min_review_score` are never pitched, even if that means fewer than three."""
    today = today or date.today()
    cfg = settings.get("trade_ideas", {}) or {}
    n_lo, n_hi = (cfg.get("per_day") or [3, 4])
    min_corr = float(cfg.get("min_pair_correlation_1y", 0.6))
    stop_mult = float(cfg.get("stop_spread_vol_multiple", 1.5))
    min_against = int(cfg.get("min_against_crowded_consensus", 1))
    max_sector = int(cfg.get("max_per_sector", 2))
    min_score = int(cfg.get("min_review_score", 7))
    rounds = int(cfg.get("review_rounds", 3))
    uni = universe(settings)
    blocked = recently_pitched(db, today, int(cfg.get("no_repeat_days", 21)))
    pairs = shortlist(settings, prices, uni, min_corr, blocked)
    pair_txt = "\n".join(f"- {p.long} / {p.short} ({uni.get(p.long)}): corr {p.corr:.2f}; spread z-score "
                         f"{p.zscore_60:+.1f} for long {p.long} / short {p.short} (reverse the sign for the "
                         f"opposite trade)" for p in pairs) or "(none passed today)"
    ctx, claims_by_id = _context(settings, states, claims, reports, snap, db, today)
    user = (f"Universe (ticker: sector): {json.dumps(uni)}\n\nPre-screened pairs:\n{pair_txt}\n\n"
            f"Recently pitched or still open, so NOT allowed in either leg: {sorted(blocked)}\n\n{ctx}")
    system = P.IDEAS_SYSTEM.format(min_against=min_against, n_candidates=8, n_final=n_hi)
    names: dict[str, str] = {}

    kept: list[Idea] = []
    tried: set[tuple[str, str]] = set()
    notes: list[str] = []            # why candidates failed, fed back to the next round
    used: set[str] = set()

    for rnd in range(rounds):
        need = n_lo - len(kept) if rnd else n_hi
        if need <= 0:
            break
        ask = user if not notes else (
            user + f"\n\nAlready accepted: {[(i.spec['long'], i.spec['short']) for i in kept]}. These were "
            f"rejected, with the reason: {notes[-14:]}. Propose {need + 4} NEW candidates that fix those "
            "weaknesses: a dated catalyst inside the horizon and evidence on both legs.")
        specs = _ask(llm, system, ask)
        round_ideas: list[Idea] = []
        per_sector = {}
        for i in kept:
            sec = uni.get(i.spec["long"], "")
            per_sector[sec] = per_sector.get(sec, 0) + 1
        for spec in specs:
            if len(round_ideas) >= need + 1:
                break
            lg, sh = spec.get("long"), spec.get("short")
            if (lg, sh) in tried:
                continue
            tried.add((lg, sh))
            if lg not in uni or sh not in uni or lg == sh:
                notes.append(f"{lg}/{sh}: not in the universe")
                continue
            if {lg, sh} & (blocked | used):
                notes.append(f"{lg}/{sh}: a leg was pitched recently or is already used")
                continue
            st = pair_stats(prices.frame, lg, sh, stop_mult=stop_mult)
            if not st or st.corr < min_corr:
                notes.append(f"{lg}/{sh}: correlation {round(st.corr, 2) if st else 'n/a'} is too low")
                continue
            sec = uni.get(lg, spec.get("sector", ""))
            if per_sector.get(sec, 0) >= max_sector:
                notes.append(f"{lg}/{sh}: already {max_sector} ideas in {sec}")
                continue
            weak = [k for k in ("what_it_is", "long_case", "short_case")
                    if not clean(spec.get(k, "") or "", claims_by_id, snap)[0].strip()]
            if weak:
                notes.append(f"{lg}/{sh}: no sourced case for {', '.join(weak)}")
                continue
            round_ideas.append(Idea(spec, st, {}))
            used.update({lg, sh})
            per_sector[sec] = per_sector.get(sec, 0) + 1
        names.update(F.company_names(db, [t for i in round_ideas for t in (i.spec["long"], i.spec["short"])]))
        for idea in round_ideas:
            _finish(idea, settings, db, llm, prices, uni, claims_by_id, snap, cfg, today, names)
            if idea.score >= min_score or idea.score == 0:
                kept.append(idea)
            else:
                used.difference_update({idea.spec["long"], idea.spec["short"]})
                notes.append(f"{idea.spec['long']}/{idea.spec['short']}: reviewer scored it {idea.score}/10: "
                             f"{idea.critique}")
                log.info("Dropped %s/%s after review (score %s): %s", idea.spec["long"], idea.spec["short"],
                         idea.score, idea.critique)
        if len(kept) >= n_lo:
            break
        log.info("Round %d: %d idea(s) cleared review so far", rnd + 1, len(kept))
    if min_against and not any(i.spec.get("against_consensus") for i in kept):
        log.info("No against-consensus idea passed today")
    if len(kept) < n_lo:
        log.warning("Only %d trade ideas cleared review today", len(kept))
    kept.sort(key=lambda i: -i.score)
    return kept[:n_hi]


def _peers(spec: dict, uni: dict[str, str], n: int) -> list[str]:
    legs = {spec["long"], spec["short"]}
    sec = uni.get(spec["long"])
    peers = [p for p in spec.get("peers", []) if uni.get(p) in (sec, uni.get(spec["short"])) and p not in legs]
    if len(peers) < n:
        peers += [t for t, s in uni.items() if s == sec and t not in legs and t not in peers
                  and not t.startswith("^") and len(t) > 2][: n - len(peers)]
    return peers[:n]


def _name(spec: dict, leg: str, names: dict[str, str]) -> str:
    n = spec.get(f"{leg}_name") or names.get(spec[leg]) or spec[leg]
    return n.replace(".", "․") if "." in n and " " not in n else n   # keep tickers like DBK.DE from auto-linking


def _fmt_price(v: float) -> str:
    return f"{v:,.2f}" if v < 10000 else f"{v:,.0f}"


def chart(pos: dict[str, dict], legs: list[tuple[str, str]]) -> bytes | None:
    """How consensus EPS for this year and next has moved over 90 days, for each leg (indexed to 100)."""
    series = []
    for ticker, name in legs:
        tr = (pos.get(ticker) or {}).get("eps_trend") or {}
        for period, style in (("0y", "-"), ("+1y", "--")):
            pts = tr.get(period)
            if pts and pts[0] and all(v is not None for v in pts):
                series.append((f"{name}, {'this year' if period == '0y' else 'next year'}", pts, style))
    if not series:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    x = [-90, -60, -30, -7, 0]
    fig, ax = plt.subplots(figsize=(7.2, 2.6), dpi=150)
    colours = {legs[0][1]: "#2e8b57", legs[1][1] if len(legs) > 1 else "": "#c0392b"}
    for label, pts, style in series:
        idx = [v / pts[0] * 100 for v in pts]
        col = next((c for n, c in colours.items() if n and label.startswith(n)), "#555555")
        ax.plot(x, idx, style, color=col, lw=1.6, marker="o", ms=3, label=label)
    ax.axhline(100, color="#999999", lw=0.8)
    ax.set_xticks(x)
    ax.set_xticklabels(["90 days ago", "60", "30", "7", "Today"], fontsize=7)
    ax.set_title("Consensus EPS estimates over the last 90 days (indexed to 100)", fontsize=9, loc="left")
    ax.tick_params(labelsize=7)
    ax.legend(fontsize=7, frameon=False, loc="best")
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(axis="y", alpha=0.2)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def _sharpen(llm, idea: Idea, data_pack: str, material: str, cfg: dict) -> dict:
    """Second pass: a sceptical PM rewrites the pitch around the data and scores it."""
    days = cfg.get("max_holding_trading_days", 20)
    system = P.SHARPEN.format(days=days, end=idea.horizon_end)
    keep = {k: idea.spec.get(k) for k in ("long", "short", "headline", "what_it_is", "long_case", "short_case",
                                         "wrong_if", "catalyst", "origin", "against_consensus")}
    user = f"The pitch:\n{json.dumps(keep, ensure_ascii=False)}\n\n{data_pack}\n\nMaterial:\n{material}"
    try:
        return llm.json(tier="trade_ideas", purpose="trade_idea_review", system=system, user=user,
                        schema=P.SHARPEN_SCHEMA, max_tokens=2500) or {}
    except (LLMUnavailable, ValueError) as e:
        log.warning("Review of %s/%s failed: %s", idea.spec["long"], idea.spec["short"], e)
        return {}


def _finish(idea: Idea, settings: Settings, db: DB, llm, prices: Prices, uni: dict[str, str],
            claims_by_id: dict[int, dict], snap: Snapshot, cfg: dict, today: date, names: dict[str, str]) -> None:
    spec, st = idea.spec, idea.stats
    lg, sh = spec["long"], spec["short"]
    n_peers = (cfg.get("comps", {}) or {}).get("peers_per_idea", [3, 4])[-1]
    idea.comps = C.build([lg, sh], _peers(spec, uni, n_peers), lambda t: F.get(db, t))
    (idea.target_spread, idea.target_return, idea.stop_return, idea.horizon_end,
     idea.target_label) = plan(st, cfg, today)
    ln, sn = _name(spec, "long", names), _name(spec, "short", names)
    rows = {r["ticker"]: r for r in idea.comps.get("rows", [])}
    med = idea.comps.get("median", {})
    pos = {t: POS.get(db, t, today) for t in (lg, sh)}
    for t, leg in ((lg, "long"), (sh, "short")):     # fall back to our own price where Yahoo has none
        q = prices.quote(t)
        if q and not pos[t].get("price"):
            pos[t]["price"] = q.last
    pos_md = POS.table([(ln, lg, pos[lg], rows.get(lg, {}), med), (sn, sh, pos[sh], rows.get(sh, {}), med)],
                       entry=(st.entry_long, st.entry_short), hedge=st.beta)

    # Second pass: review and sharpen with the data in front of the reviewer
    tick = {lg, sh}
    related = [c for c in claims_by_id.values() if tick & set(c.get("tickers") or [])]
    cited = {int(x) for k in ("long_case", "short_case", "what_it_is") for x in CITE.findall(spec.get(k, "") or "")}
    related += [claims_by_id[i] for i in cited if i in claims_by_id and claims_by_id[i] not in related]
    material = "\n".join(claim_line(c, settings.people) for c in related[:40]) or "(none beyond the pitch)"
    pack = (f"Pair statistics: 1-year correlation {st.corr:.2f}; hedge ratio {st.beta:.2f}; spread z-score "
            f"{st.zscore_60:+.1f} vs its 60-day average. Target: {idea.target_label}, worth "
            f"{idea.target_return * 100:+.1f}% hedged; stop {idea.stop_return * 100:+.1f}%; horizon to {idea.horizon_end}.\n\n"
            f"Valuation and positioning:\n{pos_md}\n\nComps:\n{idea.comps['markdown']}")
    review = _sharpen(llm, idea, pack, material, cfg) if llm is not None else {}
    if review:
        idea.score = int(review.get("score") or 0)
        idea.critique = review.get("critique", "")
        for k in ("long_case", "short_case", "wrong_if", "conviction", "conviction_reason"):
            if review.get(k):
                spec[k] = review[k]
        spec["variant_consensus"] = review.get("variant_consensus", "")
        spec["variant_ours"] = review.get("variant_ours", "")
        spec["catalysts"] = review.get("catalysts") or []
    if st.corr < float(cfg.get("min_pair_correlation_1y", 0.6)) and spec.get("conviction") == "High":
        spec["conviction"], spec["conviction_reason"] = "Medium", "the pair's correlation is only moderate"

    extra = [idea.comps["markdown"], pos_md, pack]
    texts, problems = {}, []
    for k in ("what_it_is", "variant_consensus", "variant_ours", "long_case", "short_case", "wrong_if", "catalyst"):
        txt, ids, pr = clean(spec.get(k, "") or "", claims_by_id, snap, extra)
        texts[k] = txt
        idea.cited |= ids
        problems += pr
    cats = []
    for c in spec.get("catalysts") or []:
        ev, ids, pr = clean(c.get("event", ""), claims_by_id, snap, extra + [c.get("date", "")])
        if ev:
            cats.append(f"{_nice_date(c.get('date', ''))}: {ev.rstrip('.')}")
            idea.cited |= ids
    if not cats and texts.get("catalyst"):
        cats = [texts["catalyst"].rstrip(".")]
    idea.problems = problems
    idea.texts = {**texts, "catalysts": cats}
    idea.chart = chart(pos, [(lg, ln), (sh, sn)])

    end = date.fromisoformat(idea.horizon_end).strftime("%-d %b")
    tgt = f"{idea.target_return * 100:+.1f}%".replace("-", "−")
    stp = f"{idea.stop_return * 100:+.1f}%".replace("-", "−")
    dash = (settings.get("notion.pages", {}) or {}).get("dashboard")
    setup = POS.table([(ln, lg, pos[lg], rows.get(lg, {}), med), (sn, sh, pos[sh], rows.get(sh, {}), med)],
                      entry=(st.entry_long, st.entry_short), hedge=st.beta)
    lines = [
        "::: callout 📈 gray",
        f"**Long {ln} · Short {sn}** — {spec.get('headline', '').rstrip('.')}",
        f"**The trade:** {texts['what_it_is']}",
    ]
    if texts.get("variant_consensus") and texts.get("variant_ours"):
        lines += [f"**What the market thinks:** {texts['variant_consensus']}",
                  f"**What we think:** {texts['variant_ours']}"]
    lines += [
        f"**Why {ln} (long):** {texts['long_case']}",
        f"**Why {sn} (short):** {texts['short_case']}",
    ]
    if cats:
        lines.append("**Catalysts:** " + " · ".join(cats) + ".")
    lines += [
        f"**What would prove it wrong:** {texts['wrong_if']}",
        f"🎯 **Target** <span color=\"green\">{tgt}</span> · 🛑 **Stop** <span color=\"red\">{stp}</span> · "
        f"⏳ **Horizon** {end}",
        f"*Returns are on the hedged pair. Target: {idea.target_label}. Stop: the spread falls a further "
        f"{cfg.get('stop_spread_vol_multiple', 1.5)}× its 60-day volatility.*",
        "",
        setup,
        "",
        "{{chart}}",
        "::: toggle Comps",
        idea.comps["markdown"],
        ":::",
        ("→ Tick **Following** to track this idea, or **Build out** for a model and deeper due diligence, in the "
         + (f"[Trade idea dashboard](notion:{dash})" if dash else "Ideas database") + "."),
        ":::",
    ]
    idea.markdown = "\n".join(lines)
    idea.pitch = idea.markdown


def _nice_date(s: str) -> str:
    try:
        return date.fromisoformat(s[:10]).strftime("%-d %b")
    except ValueError:
        return s


def save(db: DB, ideas: list[Idea], today: date, briefing_url: str | None = None) -> None:
    for i in ideas:
        s, st = i.spec, i.stats
        thesis = (f"{i.texts.get('what_it_is', '')} Market view: {i.texts.get('variant_consensus', '')} "
                  f"Our view: {i.texts.get('variant_ours', '')} Long: {i.texts.get('long_case', '')} "
                  f"Short: {i.texts.get('short_case', '')}")
        db.ex("INSERT INTO ideas(created,long,short,sector,origin,hedge_ratio,corr,entry_long,entry_short,"
              "entry_spread,spread_std,stop_spread,thesis,wrong,against_consensus,confidence,source_names,"
              "headline,target_spread,target_return,stop_return,horizon_end,best,worst,outcome,briefing_url,spec) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (today.isoformat(), s["long"], s["short"], s.get("sector"), s.get("origin"), st.beta, st.corr,
               st.entry_long, st.entry_short, st.spread_now, st.spread_std_60, st.stop_spread,
               thesis.strip(), i.texts.get("wrong_if"), int(bool(s.get("against_consensus"))),
               s.get("conviction"), json.dumps(sorted(i.cited)), s.get("headline"), i.target_spread,
               i.target_return, i.stop_return, i.horizon_end, 0.0, 0.0, "Open", briefing_url,
               json.dumps({**s, "markdown": i.pitch or i.markdown})))


def scorecard(db: DB) -> str:
    """Closed ideas: every idea against the ones the reader followed."""
    allc = [dict(r) for r in db.q("SELECT * FROM ideas WHERE status!='Open'")]
    n_open = db.one("SELECT COUNT(*) n FROM ideas WHERE status='Open'")["n"]
    if not allc:
        return (f"No idea has closed yet; {n_open} open. Ideas close when they hit their target or stop, "
                "or after 20 trading days.")

    def col(rows: list[dict]) -> list[str]:
        if not rows:
            return ["–"] * 5
        n = len(rows)
        hit = sum(r["outcome"] == "Hit target" for r in rows)
        stop = sum(r["outcome"] == "Stopped out" for r in rows)
        exp = n - hit - stop
        wins = sum((r["pnl"] or 0) > 0 for r in rows)
        avg = sum(r["pnl"] or 0 for r in rows) / n * 100
        return [str(hit), str(stop), str(exp), f"{wins / n * 100:.0f}%", f"{avg:+.1f}%".replace("-", "−")]

    a, f = col(allc), col([r for r in allc if r.get("following")])
    labels = ["Hit target", "Stopped out", "Expired", "Made money", "Average return (hedged)"]
    lines = ["| | All ideas | Ideas you followed |", "|---|---|---|"]
    lines += [f"| {lab} | {x} | {y} |" for lab, x, y in zip(labels, a, f)]
    lines.append(f"\n*{len(allc)} closed, {n_open} open.*")
    return "\n".join(lines)
