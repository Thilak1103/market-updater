"""Trade ideas: candidates from Claude Opus, statistics and comps from code, tracking in SQLite/Notion."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime

from ..analysis.checks import check_section
from ..analysis.views import DebateState
from ..db import DB
from ..llm import LLMUnavailable
from ..market import comps as C
from ..market import fundamentals as F
from ..market.pairs import PairStats, pair_pnl, pair_stats, stopped
from ..market.prices import Prices
from ..market.snapshot import Snapshot
from ..settings import Settings
from ..util import log
from . import prompts as P
from .sections import claim_line, select


@dataclass
class Idea:
    spec: dict
    stats: PairStats
    comps: dict
    markdown: str = ""
    problems: list[str] = field(default_factory=list)
    cited: set[int] = field(default_factory=set)


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


def update_open(db: DB, prices: Prices, settings: Settings, today: date) -> list[str]:
    """Mark open ideas to market; close on stop, expiry. Returns one-line updates."""
    max_days = int(settings.get("trade_ideas.max_holding_trading_days", 20))
    lines = []
    for idea in open_ideas(db):
        ql, qs = prices.quote(idea["long"]), prices.quote(idea["short"])
        if not ql or not qs:
            continue
        pnl = pair_pnl(idea["entry_long"], idea["entry_short"], idea["hedge_ratio"], ql.last, qs.last)
        status, reason = "Open", None
        held = len(prices.frame.loc[prices.frame.index > idea["created"][:10]]) if not prices.frame.empty else 0
        if stopped(idea["hedge_ratio"], idea["stop_spread"], ql.last, qs.last):
            status, reason = "Stopped out", "spread hit the stop"
        elif held >= max_days:
            status, reason = "Expired", f"{max_days} trading days elapsed"
        db.ex("UPDATE ideas SET pnl=?, status=?, closed=?, close_reason=? WHERE id=?",
              (pnl, status, today.isoformat() if status != "Open" else None, reason, idea["id"]))
        tag = f" ({status.lower()}: {reason})" if reason else ""
        lines.append(f"- Long {idea['long']} / short {idea['short']}: {pnl * 100:+.1f}% hedged since "
                     f"{idea['created'][:10]}{tag}")
    return lines


def _context(settings: Settings, states: list[DebateState], claims: list[dict], reports: list,
             snap: Snapshot) -> tuple[str, dict[int, dict]]:
    people = settings.people
    idea_sources = {s.name.lower() for s in settings.sources if s.ideas or s.contrarian}
    pitches = [c for c in claims if (c.get("source_name") or "").lower() in idea_sources
               and c.get("tickers")]
    chosen = select(claims, people, limit=80)
    chosen += [c for c in pitches if c not in chosen][:30]
    blocks = []
    for s in states[:8]:
        blocks.append(f"Debate {s.key} ({s.question}): majority {s.majority} {int(s.share * 100)}%, "
                      f"crowded={s.crowded}, cracking={s.cracking}, dissenters="
                      f"{[d['speaker'] for d in s.dissenters]}, changed={[d['speaker'] for d in s.changed]}")
    rep_lines = []
    for r in reports:
        rep_lines.append(f"{r.ticker}: move {r.move_pct}, sector {r.sector_move_pct}, usual abs move "
                         f"{r.avg_abs_move_pct}, reaction_fit={getattr(r, 'reaction_fit', None)}")
    trig = [f"{t.sector}: {t.reason}" for t in snap.triggers]
    text = ("Debates:\n" + "\n".join(blocks) + "\n\nEarnings reactions:\n" + "\n".join(rep_lines)
            + "\n\nSector and stock triggers:\n" + "\n".join(trig)
            + "\n\nClaims:\n" + "\n".join(claim_line(c, people) for c in chosen))
    return text, {c["id"]: c for c in chosen}


def generate(settings: Settings, db: DB, llm, prices: Prices, states: list[DebateState],
             claims: list[dict], reports: list, snap: Snapshot) -> list[Idea]:
    cfg = settings.get("trade_ideas", {}) or {}
    n_lo, n_hi = (cfg.get("per_day") or [3, 4])
    min_corr = float(cfg.get("min_pair_correlation_1y", 0.5))
    stop_mult = float(cfg.get("stop_spread_vol_multiple", 1.5))
    min_against = int(cfg.get("min_against_crowded_consensus", 1))
    max_sector = int(cfg.get("max_per_sector", 2))
    uni = universe(settings)
    open_pairs = {(i["long"], i["short"]) for i in open_ideas(db)}
    ctx, claims_by_id = _context(settings, states, claims, reports, snap)
    user = (f"Universe (ticker: sector): {json.dumps(uni)}\n\nOpen ideas (do not repeat): "
            f"{sorted(open_pairs)}\n\n{ctx}")
    system = P.IDEAS_SYSTEM.format(min_against=min_against, n_candidates=8, n_final=n_hi)
    try:
        res = llm.json(tier="trade_ideas", purpose="trade_ideas", system=system, user=user,
                       schema=P.IDEAS_SCHEMA, max_tokens=6000)
    except LLMUnavailable as e:
        log.warning("No trade ideas today: %s", e)
        return []
    picked: list[Idea] = []
    per_sector: dict[str, int] = {}
    for spec in res.get("ideas", []):
        lg, sh = spec.get("long"), spec.get("short")
        if lg not in uni or sh not in uni or lg == sh or (lg, sh) in open_pairs:
            continue
        st = pair_stats(prices.frame, lg, sh, stop_mult=stop_mult)
        if not st or st.corr < min_corr:
            log.info("Rejected %s/%s: correlation %s", lg, sh, st.corr if st else "n/a")
            continue
        sec = uni.get(lg, spec.get("sector", ""))
        if per_sector.get(sec, 0) >= max_sector:
            continue
        picked.append(Idea(spec, st, {}))
        per_sector[sec] = per_sector.get(sec, 0) + 1
    # enforce the against-consensus minimum within the final selection
    final = picked[:n_hi]
    if min_against and not any(i.spec.get("against_consensus") for i in final):
        contra = next((i for i in picked[n_hi:] if i.spec.get("against_consensus")), None)
        if contra and final:
            final[-1] = contra
    if len(final) < n_lo:
        log.info("Only %d ideas passed the checks today", len(final))
    for idea in final:
        _finish(idea, settings, db, prices, uni, claims_by_id, snap)
    return final


def _peers(spec: dict, uni: dict[str, str], db: DB, n: int) -> list[str]:
    legs = {spec["long"], spec["short"]}
    peers = [p for p in spec.get("peers", []) if p in uni and p not in legs]
    if len(peers) < n:
        sec = uni.get(spec["long"])
        peers += [t for t, s in uni.items() if s == sec and t not in legs and t not in peers
                  and not t.startswith("^") and len(t) > 2][: n - len(peers)]
    return peers[:n]


def _finish(idea: Idea, settings: Settings, db: DB, prices: Prices, uni: dict[str, str],
            claims_by_id: dict[int, dict], snap: Snapshot) -> None:
    spec, st = idea.spec, idea.stats
    n_peers = (settings.get("trade_ideas.comps.peers_per_idea") or [3, 4])[-1]
    peers = _peers(spec, uni, db, n_peers)

    def ret(t, k):
        return prices.returns(t, 21) if k == "1m" else prices.ytd(t)

    idea.comps = C.build([spec["long"], spec["short"]], peers, lambda t: F.get(db, t), ret)
    text_fields = " ".join(spec.get(k, "") for k in ("thesis", "why_now", "consensus", "wrong_if"))
    clean, problems = check_section(text_fields, claims_by_id, snap.numbers,
                                    [idea.comps["markdown"]])
    idea.problems = problems
    th, _ = check_section(spec.get("thesis", ""), claims_by_id, snap.numbers, [idea.comps["markdown"]])
    wn, _ = check_section(spec.get("why_now", ""), claims_by_id, snap.numbers, [idea.comps["markdown"]])
    cs, _ = check_section(spec.get("consensus", ""), claims_by_id, snap.numbers, [idea.comps["markdown"]])
    wr, _ = check_section(spec.get("wrong_if", ""), claims_by_id, snap.numbers, [idea.comps["markdown"]])
    from ..analysis.checks import CITE
    idea.cited = {int(x) for x in CITE.findall(th + wn + cs + wr)}
    prem = C.premium_sentence(idea.comps["premium"])
    tag = "Against consensus" if spec.get("against_consensus") else "With consensus"
    stats_line = (f"Hedge ratio {st.beta:.2f} (short {st.beta:.2f} of {spec['short']} per 1 of "
                  f"{spec['long']}); 1-year correlation {st.corr:.2f}; spread z-score {st.zscore_60:+.1f} "
                  f"vs 60 days; stop if the spread falls {settings.get('trade_ideas.stop_spread_vol_multiple', 1.5)}x "
                  f"its 60-day volatility below entry.")
    idea.markdown = "\n".join([
        f"### {spec['headline']}",
        f"**{spec['origin']}** · {tag} · Confidence: {spec.get('confidence', 'Medium')}"
        + (f" ({spec['confidence_reason']})" if spec.get("confidence_reason") else ""),
        f"- **The pair isolates:** {spec.get('isolates', '')}",
        f"- **Thesis:** {th}",
        f"- **Why now:** {wn}",
        f"- **Consensus:** {cs}",
        f"- **What would prove it wrong:** {wr}",
        f"- **Statistics:** {stats_line}",
        f"- **Valuation:** {prem or 'see comps'}",
        "::: toggle Comps",
        idea.comps["markdown"],
        ":::",
    ])


def save(db: DB, ideas: list[Idea], today: date) -> None:
    for i in ideas:
        s, st = i.spec, i.stats
        db.ex("INSERT INTO ideas(created,long,short,sector,origin,hedge_ratio,corr,entry_long,entry_short,"
              "entry_spread,spread_std,stop_spread,thesis,wrong,against_consensus,confidence,source_names) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (today.isoformat(), s["long"], s["short"], s.get("sector"), s.get("origin"), st.beta, st.corr,
               st.entry_long, st.entry_short, st.spread_now, st.spread_std_60, st.stop_spread,
               s.get("thesis"), s.get("wrong_if"), int(bool(s.get("against_consensus"))),
               s.get("confidence"), json.dumps(sorted(i.cited))))


def scorecard(db: DB, since: str) -> str:
    rows = [dict(r) for r in db.q("SELECT * FROM ideas WHERE created>=? OR closed>=?", (since, since))]
    if not rows:
        return "- No ideas tracked yet."
    closed = [r for r in rows if r["status"] != "Open"]
    allc = [dict(r) for r in db.q("SELECT * FROM ideas WHERE status!='Open'")]
    wins = [r for r in allc if (r["pnl"] or 0) > 0]
    lines = [f"- This week: {len(rows)} ideas tracked, {len(closed)} closed.",
             f"- All time: {len(allc)} closed, hit rate {len(wins) / len(allc) * 100:.0f}%, average "
             f"{sum(r['pnl'] or 0 for r in allc) / len(allc) * 100:+.1f}% hedged." if allc else
             "- All time: none closed yet."]
    by_origin: dict[str, list[float]] = {}
    for r in allc:
        by_origin.setdefault(r["origin"] or "Other", []).append(r["pnl"] or 0)
    for o, v in sorted(by_origin.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"- {o}: {len(v)} closed, average {sum(v) / len(v) * 100:+.1f}%")
    for r in rows:
        lines.append(f"- Long {r['long']} / short {r['short']} ({r['status']}): {(r['pnl'] or 0) * 100:+.1f}%")
    return "\n".join(lines)
