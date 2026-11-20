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
from datetime import date

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
    if st.zscore_60 <= -0.75 and st.spread_mean_60 - st.spread_now >= std:
        target = st.spread_mean_60
        label = f"spread back to its 60-day average (now {abs(st.zscore_60):.1f} standard deviations below it)"
    else:
        target = st.spread_now + t_mult * std
        label = f"spread widens {t_mult:g}× its 60-day volatility in the long's favour"
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
             snap: Snapshot) -> tuple[str, dict[int, dict]]:
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
    return text, {c["id"]: c for c in chosen}


def _ask(llm, system: str, user: str) -> list[dict]:
    try:
        res = llm.json(tier="trade_ideas", purpose="trade_ideas", system=system, user=user,
                       schema=P.IDEAS_SCHEMA, max_tokens=8000)
        return res.get("ideas", []) or []
    except (LLMUnavailable, ValueError) as e:
        log.warning("Trade-idea call failed: %s", e)
        return []


def generate(settings: Settings, db: DB, llm, prices: Prices, states: list[DebateState],
             claims: list[dict], reports: list, snap: Snapshot, today: date | None = None) -> list[Idea]:
    today = today or date.today()
    cfg = settings.get("trade_ideas", {}) or {}
    n_lo, n_hi = (cfg.get("per_day") or [3, 4])
    min_corr = float(cfg.get("min_pair_correlation_1y", 0.6))
    floor_corr = float(cfg.get("fallback_min_correlation_1y", 0.5))
    stop_mult = float(cfg.get("stop_spread_vol_multiple", 1.5))
    min_against = int(cfg.get("min_against_crowded_consensus", 1))
    max_sector = int(cfg.get("max_per_sector", 2))
    uni = universe(settings)
    opened = open_ideas(db)
    open_pairs = {(i["long"], i["short"]) for i in opened}
    pairs = shortlist(settings, prices, uni, min_corr, set())
    pair_txt = "\n".join(f"- {p.long} / {p.short} ({uni.get(p.long)}): corr {p.corr:.2f}; spread z-score "
                         f"{p.zscore_60:+.1f} for long {p.long} / short {p.short} (reverse the sign for the "
                         f"opposite trade)" for p in pairs) or "(none passed today)"
    ctx, claims_by_id = _context(settings, states, claims, reports, snap)
    user = (f"Universe (ticker: sector): {json.dumps(uni)}\n\nPre-screened pairs:\n{pair_txt}\n\n"
            f"Open ideas (do not repeat): {sorted(open_pairs)}\n\n{ctx}")
    system = P.IDEAS_SYSTEM.format(min_against=min_against, n_candidates=8, n_final=n_hi)

    picked: list[Idea] = []
    rejected: list[str] = []
    used: set[str] = set()
    per_sector: dict[str, int] = {}

    pool = n_hi + 1          # one spare: the review step can drop the weakest

    def consider(specs: list[dict], corr_floor: float, sector_cap: int) -> None:
        for spec in specs:
            if len(picked) >= pool:
                return
            lg, sh = spec.get("long"), spec.get("short")
            if lg not in uni or sh not in uni or lg == sh or (lg, sh) in open_pairs:
                rejected.append(f"{lg}/{sh}: not in the universe or already open")
                continue
            if lg in used or sh in used:
                rejected.append(f"{lg}/{sh}: a leg is already used today")
                continue
            st = pair_stats(prices.frame, lg, sh, stop_mult=stop_mult)
            if not st or st.corr < corr_floor:
                rejected.append(f"{lg}/{sh}: correlation {st.corr if st else 'n/a'}")
                continue
            sec = uni.get(lg, spec.get("sector", ""))
            if per_sector.get(sec, 0) >= sector_cap:
                rejected.append(f"{lg}/{sh}: already {sector_cap} ideas in {sec}")
                continue
            weak = [k for k in ("what_it_is", "long_case", "short_case")
                    if not clean(spec.get(k, "") or "", claims_by_id, snap)[0].strip()]
            if weak:
                rejected.append(f"{lg}/{sh}: no sourced case for {', '.join(weak)}")
                continue
            if st.corr < min_corr and spec.get("conviction") == "High":
                spec["conviction"], spec["conviction_reason"] = "Medium", "the pair's correlation is only moderate"
            picked.append(Idea(spec, st, {}))
            used.update({lg, sh})
            per_sector[sec] = per_sector.get(sec, 0) + 1

    specs = _ask(llm, system, user)
    consider(specs, min_corr, max_sector)
    if len(picked) < n_lo:
        log.info("Only %d ideas passed; asking for more", len(picked))
        more = (f"\n\nThese candidates were rejected: {rejected[:12]}. Already accepted: "
                f"{[(i.spec['long'], i.spec['short']) for i in picked]}. Give {n_lo - len(picked) + 3} new "
                f"candidates from the pre-screened pairs, in sectors not yet used where possible.")
        consider(_ask(llm, system, user + more), min_corr, max_sector)
    if len(picked) < n_lo:          # last resort: relax the limits, and say so through conviction
        consider(specs, floor_corr, max_sector + 1)
    if min_against and not any(i.spec.get("against_consensus") for i in picked):
        log.info("No against-consensus idea passed today")
    if len(picked) < n_lo:
        log.warning("Only %d trade ideas today", len(picked))
    names = F.company_names(db, [t for i in picked for t in (i.spec["long"], i.spec["short"])])
    for idea in picked:
        _finish(idea, settings, db, llm, prices, uni, claims_by_id, snap, cfg, today, names)
    # Keep the strongest: everything the reviewer scored 6+, topped up to the daily minimum.
    min_score = int(cfg.get("min_review_score", 6))
    ranked = sorted(picked, key=lambda i: -i.score)
    keep = [i for i in ranked if i.score >= min_score or i.score == 0]
    if len(keep) < n_lo:
        keep = ranked[:n_lo]
        for i in keep:             # kept only to reach the minimum: say so
            if 0 < i.score < min_score:
                _relabel_low(i, i.critique or "the reviewer found the evidence thin")
    for i in ranked:
        if i not in keep[:n_hi]:
            log.info("Dropped %s/%s after review (score %s): %s", i.spec["long"], i.spec["short"], i.score, i.critique)
    return keep[:n_hi]


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


def chart(prices: Prices, st: PairStats, target: float, long_name: str, short_name: str) -> bytes | None:
    """A one-year chart of the spread with entry, target and stop marked."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None
    df = prices.frame[[st.long, st.short]].dropna().iloc[-253:]
    if len(df) < 60:
        return None
    spread = np.log(df[st.long]) - st.beta * np.log(df[st.short])
    rel = (np.exp(spread - st.spread_now) - 1) * 100       # % from today's level, hedged
    fig, ax = plt.subplots(figsize=(7.2, 2.8), dpi=150)
    ax.plot(rel.index, rel.values, color="#3b6ea5", lw=1.4)
    for lvl, col, lab in ((0, "#555555", "Entry"), ((math.exp(target - st.spread_now) - 1) * 100, "#2e8b57", "Target"),
                          ((math.exp(st.stop_spread - st.spread_now) - 1) * 100, "#c0392b", "Stop")):
        ax.axhline(lvl, color=col, lw=1, ls="--")
        ax.text(rel.index[0], lvl, f" {lab}", color=col, va="bottom", fontsize=8)
    ax.set_title(f"Long {long_name} / short {short_name}: hedged spread, % from entry", fontsize=9, loc="left")
    ax.tick_params(labelsize=7)
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
    pos_md = POS.table([(ln, lg, pos[lg], rows.get(lg, {}), med), (sn, sh, pos[sh], rows.get(sh, {}), med)])

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
    idea.chart = chart(prices, st, idea.target_spread, ln, sn)

    conv = spec.get("conviction", "Medium")
    conv_txt = f'<span color="{CONVICTION_COLOR.get(conv, "gray")}">{conv}</span>'
    if spec.get("conviction_reason"):
        conv_txt += f" ({spec['conviction_reason'].rstrip('.')})"
    end = date.fromisoformat(idea.horizon_end).strftime("%-d %b")
    horizon = f"{cfg.get('max_holding_trading_days', 20)} trading days (to {end})"
    tgt = f"{idea.target_return * 100:+.1f}%".replace("-", "−")
    stp = f"{idea.stop_return * 100:+.1f}%".replace("-", "−")
    entry = (f"{ln} {_fmt_price(st.entry_long)} / {sn} {_fmt_price(st.entry_short)} "
             f"({st.beta:.2f} of {sn} short per 1 of {ln} long)")
    dash = (settings.get("notion.pages", {}) or {}).get("dashboard")
    lines = [
        "::: callout 📈 gray",
        f"**Long {ln} · Short {sn}** — {spec.get('headline', '').rstrip('.')}",
        f"**The trade:** {texts['what_it_is']}",
    ]
    if texts.get("variant_consensus") and texts.get("variant_ours"):
        lines.append(f"**What the market thinks:** {texts['variant_consensus']} **What we think:** {texts['variant_ours']}")
    lines += [
        f"**Why {sn} (short):** {texts['short_case']}",
        f"**Why {ln} (long):** {texts['long_case']}",
    ]
    if cats:
        lines.append("**Catalysts:** " + " · ".join(cats) + ".")
    lines += [
        f"**What would prove it wrong:** {texts['wrong_if']}",
        "| Entry | Target | Stop | Horizon | Conviction |",
        "|---|---|---|---|---|",
        f"| {entry} | <span color=\"green\">{tgt}</span> hedged ({idea.target_label}) | <span color=\"red\">{stp}</span> "
        f"(spread falls a further {cfg.get('stop_spread_vol_multiple', 1.5)}× its 60-day volatility) | {horizon} | {conv_txt} |",
        pos_md,
        "::: toggle Comps",
        idea.comps["markdown"],
        ":::",
        "{{chart}}",
        ("→ Tick **Following** to track this idea, or **Build out** for a model and deeper due diligence, in the "
         + (f"[Trade idea dashboard](notion:{dash})" if dash else "Ideas database") + "."),
        ":::",
    ]
    idea.markdown = "\n".join(lines)
    idea.pitch = idea.markdown


def _relabel_low(idea: Idea, reason: str) -> None:
    import re
    old = re.search(r"\| <span color=\"\w+\">(Low|Medium|High)</span>[^|]*\|$", idea.markdown, re.M)
    new = f'| <span color="gray">Low</span> ({reason.rstrip(".")}) |'
    if old:
        idea.markdown = idea.markdown[:old.start()] + new + idea.markdown[old.end():]
        idea.pitch = idea.markdown
    idea.spec["conviction"] = "Low"


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
