"""Write each part of the briefing from claims and code-built tables (Claude Sonnet).

Every drafted sentence goes through check_section (numbers and citations must trace to a
source) and tidy (no talk about inputs, no half-written citations, no cut-off sentences).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..analysis.checks import CITE, check_section, tidy
from ..analysis.views import DebateState
from ..llm import LLMUnavailable
from ..market.snapshot import Snapshot
from ..settings import Sector, Settings
from ..util import log, truncate
from . import prompts as P

ICONS = {"tmt": "💻", "autos": "🚗", "staples_retail": "🛒", "financials": "🏦"}
TYPE_LABEL = {"substack": "Substack", "podcast": "Podcast", "youtube": "Video", "rss": "News",
              "headline": "News", "web_page": "Research", "press_mentions": "News", "email": "Newsletter",
              "inbox": "Saved by you", "bluesky": "Bluesky", "api": "Data"}
READING_GROUPS = (("✍️ Substack and research", ("substack", "web_page", "email", "inbox", "bluesky")),
                  ("🎙 Podcasts and video", ("podcast", "youtube")),
                  ("📰 News", None))


@dataclass
class Section:
    title: str
    markdown: str
    cited: set[int] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)


@dataclass
class MarketStory:
    headline: str
    mood: str
    bullets: list[str]
    so_what: dict[str, str]
    cited: set[int] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)


@dataclass
class SectorOut:
    sector: Sector
    blurb: str
    reading: list[tuple[dict, str]]          # (item, summary), best first
    filtered: str
    cited: set[int] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)
    reading_url: str | None = None


@dataclass
class TableOut:
    title: str
    rows: list[dict]
    cited: set[int] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)


# -- helpers --------------------------------------------------------------------

def claim_line(c: dict, people: dict) -> str:
    person = people.get((c.get("speaker") or "").lower())
    meta = {
        "kind": c["kind"], "speaker": c.get("speaker"), "via": c.get("via") or c.get("source_name"),
        "date": (c.get("published") or "")[:10], "stance": c.get("stance") or None,
        "debate": c.get("debate") or None, "priority": bool(person and person.priority),
        "contrarian": bool(person and person.contrarian), "subscriber": bool(c.get("is_subscriber")),
        "repeat": bool(c.get("repeat_of")),
    }
    meta = {k: v for k, v in meta.items() if v not in (None, False, "")}
    return (f"[c{c['id']}] {json.dumps(meta, ensure_ascii=False)} {c['claim']} "
            f"Evidence: \"{truncate(c.get('evidence') or '', 220)}\"")


def select(claims: list[dict], people: dict, *, sectors: set[str] | None = None,
           tickers: set[str] | None = None, limit: int = 70) -> list[dict]:
    out = []
    for c in claims:
        tick = set(c.get("tickers") or [])
        if sectors and c.get("sector") not in sectors and not (tickers and tick & tickers):
            continue
        out.append(c)

    def rank(c):
        p = people.get((c.get("speaker") or "").lower())
        return (0 if p and p.priority else 1, 0 if c["kind"] in ("data", "fact") else 1,
                1 if c.get("repeat_of") else 0, -(c["id"]))
    out.sort(key=rank)
    return out[:limit]


def _claims_block(claims: list[dict], people: dict) -> str:
    return "\n".join(claim_line(c, people) for c in claims) or "(nothing today)"


def clean(text: str, claims_by_id: dict[int, dict], snap: Snapshot,
          extra: list[str] | None = None) -> tuple[str, set[int], list[str]]:
    """Check numbers and citations, then tidy. Returns (text, cited ids, problems)."""
    checked, problems = check_section(text or "", claims_by_id, snap.numbers, extra)
    out = tidy(checked)
    return out, {int(x) for x in CITE.findall(out)}, problems


def _json(llm, *, system: str, user: str, schema: dict, purpose: str, max_tokens: int = 3000) -> dict | None:
    try:
        return llm.json(tier="write", purpose=purpose, system=system, user=user, schema=schema,
                        max_tokens=max_tokens)
    except (LLMUnavailable, ValueError, json.JSONDecodeError) as e:
        log.warning("%s not written: %s", purpose, e)
        return None


def _write(llm, system: str, user: str, claims_by_id: dict[int, dict], snapshot: Snapshot,
           title: str, extra_texts: list[str] | None = None, purpose: str = "write",
           max_tokens: int = 3500) -> Section:
    try:
        md = llm.text(tier="write", purpose=purpose, system=system, user=user, max_tokens=max_tokens)
    except LLMUnavailable as e:
        log.warning("Section %s not written: %s", title, e)
        return Section(title, "", set(), [str(e)])
    text, cited, problems = clean(md.strip().replace("[m]", ""), claims_by_id, snapshot, extra_texts)
    return Section(title, text, cited, problems)


def move_word(chg: float) -> str:
    return "rose" if chg > 0 else "fell"


def pct(v: float | None, color: bool = True) -> str:
    if v is None:
        return "–"
    txt = f"{v:+.1f}%".replace("-", "−")
    if not color or abs(v) < 0.05:
        return txt
    return f'<span color="{"green" if v > 0 else "red"}">{txt}</span>'


# -- the opening: top line and the numbers that matter --------------------------

def key_table_text(snap: Snapshot) -> str:
    return "\n".join(f"- {r['name']}: {r['level']} ({r['raw']})" for r in snap.key_rows)


def market_story(llm, settings: Settings, snap: Snapshot, claims: list[dict]) -> MarketStory | None:
    people = settings.people
    chosen = select(claims, people, sectors={"macro"}, limit=60)
    movers = [c for c in select(claims, people, limit=200) if c["kind"] in ("data", "fact")]
    chosen += [c for c in movers if c not in chosen][:50]
    trig = "\n".join(f"- {t.sector}: {t.reason}" for t in snap.triggers) or "(none)"
    user = (f"Key numbers (as of {snap.asof_label}; rows in this order):\n{key_table_text(snap)}\n\n"
            f"Full market table:\n{snap.overview_md}\n\nRates, credit and macro:\n{snap.macro_md}\n\n"
            f"Sector moves that crossed alert thresholds:\n{trig}\n\nMaterial:\n{_claims_block(chosen, people)}")
    res = _json(llm, system=P.MARKET_STORY, user=user, schema=P.MARKET_STORY_SCHEMA,
                purpose="write_market_story", max_tokens=3000)
    if not res:
        return None
    by_id = {c["id"]: c for c in chosen}
    extra = [key_table_text(snap), snap.overview_md, snap.macro_md, trig]
    cited: set[int] = set()
    problems: list[str] = []
    bullets = []
    for b in res.get("bullets", [])[:6]:
        lead = tidy(b.get("lead", "")).rstrip(".:")
        text, ids, pr = clean(b.get("text", ""), by_id, snap, extra)
        problems += pr
        if text and lead:
            bullets.append(f"- **{lead}:** {text}")
            cited |= ids
    so_what = {}
    for name, note in (res.get("so_what") or {}).items():
        text, ids, pr = clean(note, by_id, snap, extra)
        so_what[name] = text
        cited |= ids
        problems += pr
    headline = tidy(res.get("headline", "")).strip()
    return MarketStory(headline, res.get("mood", "mixed"), bullets, so_what, cited, problems)


def headline_moves(snap: Snapshot) -> str:
    """'The S&P 500 fell 2.0% and the Nasdaq 100 2.2%...' built from the key rows, in code."""
    want = ["S&P 500", "Nasdaq 100", "FTSE 100", "Euro Stoxx 50", "Nikkei 225"]
    bits = []
    for name in want:
        r = next((x for x in snap.key_rows if x["name"] == name), None)
        if r:
            bits.append(f"{name} {r['move']}")
    vix = next((x for x in snap.key_rows if x["name"] == "VIX"), None)
    if vix:
        bits.append(f"VIX {vix['level']}")
    return " · ".join(bits)


# -- core sectors ---------------------------------------------------------------

def _items_block(chosen: list[dict], people: dict) -> tuple[str, dict[int, dict]]:
    items: dict[int, dict] = {}
    for c in chosen:
        it = items.setdefault(c["item_id"], {
            "id": c["item_id"], "title": c.get("item_title") or "", "url": c.get("url") or "",
            "source": c.get("source_name") or "", "type": c.get("source_type") or "rss", "claims": []})
        it["claims"].append(c)
    numbered = {i + 1: it for i, it in enumerate(items.values())}
    lines = []
    for n, it in numbered.items():
        lines.append(f"Item {n} ({TYPE_LABEL.get(it['type'], 'News')} · {it['source']}): {it['title']}")
        lines += ["  " + claim_line(c, people) for c in it["claims"]]
    return "\n".join(lines), numbered


def sector_section(llm, settings: Settings, sector: Sector, snap: Snapshot, claims: list[dict],
                   words: int) -> SectorOut:
    people = settings.people
    chosen = select(claims, people, sectors={sector.key}, tickers=set(sector.watchlist), limit=80)
    block, numbered = _items_block(chosen, people)
    user = (f"Moves today:\n{snap.sector_tables.get(sector.key, '')}\n\n"
            f"Live debates for this sector: {', '.join(sector.debates) or 'none'}\n\nItems:\n{block}")
    system = P.SECTOR.format(sector=sector.name, subsectors="; ".join(sector.subsectors) or "all",
                             words=words, max_items=10)
    res = _json(llm, system=system, user=user, schema=P.SECTOR_SCHEMA, purpose=f"write_{sector.key}",
                max_tokens=3000)
    by_id = {c["id"]: c for c in chosen}
    extra = [snap.sector_tables.get(sector.key, "")]
    if not res:
        return SectorOut(sector, "", [], "", set(), ["not written"])
    blurb, cited, problems = clean(res.get("blurb", ""), by_id, snap, extra)
    reading = []
    for r in res.get("reading", [])[:12]:
        it = numbered.get(r.get("item"))
        if not it or not it["url"]:
            continue
        summary, ids, pr = clean(r.get("summary", ""), by_id, snap, extra)
        problems += pr
        if summary:
            reading.append((it, summary))
            cited |= ids
    filtered = tidy(res.get("filtered", "") or "")
    return SectorOut(sector, blurb, reading, filtered, cited, problems)


def reading_markdown(out: SectorOut, snap: Snapshot, names: dict[str, str]) -> str:
    """The body of a sector's reading page."""
    icon = ICONS.get(out.sector.key, "📚")
    parts = [f"::: callout {icon} blue", f"**Today in {out.sector.name}:** {out.blurb or 'A quiet day.'}", ":::"]
    rows = [r for r in snap.sector_rows.get(out.sector.key, [])]
    if rows:
        rows.sort(key=lambda r: abs(r["chg"]), reverse=True)
        parts += ["| Name | Day | 1 month | YTD |", "|---|---|---|---|"]
        for r in rows[:8]:
            parts.append(f"| {names.get(r['ticker'], r['ticker'])} | {pct(r['chg'])} | {pct(r['r1m'], False)} | "
                         f"{pct(r['ytd'], False)} |")
    for heading, types in READING_GROUPS:
        group = [(it, s) for it, s in out.reading
                 if (types is None and not any(it["type"] in t for _, t in READING_GROUPS if t))
                 or (types and it["type"] in types)]
        if not group:
            continue
        parts.append(f"## {heading}")
        for it, summary in group:
            label = f"{it['source']} ({TYPE_LABEL.get(it['type'], 'News')})"
            if types is None:
                parts.append(f"- [**{it['title']}**]({it['url']}) ({it['source']}): {summary}")
            else:
                ic = "🎧" if it["type"] in ("podcast", "youtube") else "📄"
                parts += [f"::: callout {ic} gray", f"[**{it['title']}**]({it['url']}) · {label}", summary, ":::"]
    if not out.reading:
        parts.append("Nothing worth a recommendation today.")
    if out.filtered:
        parts += ["::: callout 🧹 gray", f"*Filtered out: {out.filtered.rstrip('.')}.*", ":::"]
    return "\n".join(parts)


# -- elsewhere, consensus check, deep dive -------------------------------------

def elsewhere_section(llm, settings: Settings, snap: Snapshot, claims: list[dict], words: int = 0) -> TableOut | None:
    trig = [t for t in snap.triggers if not (settings.sector(t.sector) and settings.sector(t.sector).core)]
    if not trig:
        return None
    keys = list(dict.fromkeys(t.sector for t in sorted(trig, key=lambda t: -t.magnitude)))[:8]
    people = settings.people
    tick = {t for k in keys if (s := settings.sector(k)) for t in s.watchlist + s.proxies}
    chosen = select(claims, people, sectors=set(keys) | {"elsewhere"}, tickers=tick, limit=50)
    names = {k: (settings.sector(k).name if settings.sector(k) else k.title()) for k in keys}
    trig_txt = "\n".join(f"- {names[t.sector]}: {t.reason}" for t in trig)
    user = (f"Sectors and their moves:\n{trig_txt}\n\nTables:\n"
            + "\n\n".join(snap.sector_tables.get(k, "") for k in keys)
            + f"\n\nMaterial:\n{_claims_block(chosen, people)}")
    res = _json(llm, system=P.ELSEWHERE, user=user, schema=P.ELSEWHERE_SCHEMA, purpose="write_elsewhere",
                max_tokens=1500) or {"rows": []}
    whys = {r.get("sector", "").lower(): r.get("why", "") for r in res.get("rows", [])}
    by_id = {c["id"]: c for c in chosen}
    spx = snap.quotes.get("^GSPC")
    rows, cited, problems = [], set(), []
    for k in keys:
        srows = snap.sector_rows.get(k, [])
        prox = [r for r in srows if r["proxy"]][:2]
        moves = [f"{r['ticker']} {pct(r['chg'])}" for r in prox]
        moves += [t.reason.replace(" single-stock move", "") for t in trig if t.sector == k and "single-stock" in t.reason]
        why, ids, pr = clean(whys.get(names[k].lower(), ""), by_id, snap, [trig_txt])
        problems += pr
        if not why:
            chg = prox[0]["chg"] if prox else 0.0
            with_mkt = spx and (spx.change_pct > 0) == (chg > 0) and abs(spx.change_pct) >= 0.5
            why = f"No clear catalyst; it {move_word(chg)} with the market" if with_mkt else "No clear catalyst in today's news"
        rows.append({"sector": names[k], "move": ", ".join(moves) or "–", "why": why})
        cited |= ids
    return TableOut("Elsewhere", rows, cited, problems)


def consensus_section(llm, settings: Settings, states: list[DebateState], claims_by_id: dict[int, dict],
                      snap: Snapshot) -> TableOut | None:
    states = [s for s in states if s.speakers >= 3 and s.majority][:5]
    if not states:
        return None
    blocks, ids = [], {}
    for s in states:
        lines = [f"Debate {s.key}: {s.question} (majority {s.majority}, {int(s.share * 100)}% of "
                 f"{s.speakers} voices; crowded={s.crowded}; cracking={s.cracking})"]
        for v in s.dissenters + s.changed + s.latest[:6]:
            c = claims_by_id.get(v.get("claim_id"))
            if c:
                ids[c["id"]] = c
                tag = "dissenter" if v in s.dissenters else "changed view" if v in s.changed else v.get("stance")
                lines.append(f"  [c{c['id']}] {v['speaker']} ({tag}): {c['claim']}")
        blocks.append("\n".join(lines))
    res = _json(llm, system=P.CONSENSUS, user="\n\n".join(blocks), schema=P.CONSENSUS_SCHEMA,
                purpose="write_consensus", max_tokens=1200) or {"rows": []}
    notes = {r.get("key"): r.get("note", "") for r in res.get("rows", [])}
    rows, cited, problems = [], set(), []
    for s in states:
        lean = {"bull": "Leaning bull", "bear": "Leaning bear"}.get(s.majority, f"Leaning {s.majority}")
        crowd = f"{lean} ({int(s.share * 100)}% of {s.speakers} voices)" + (", crowded" if s.crowded else "")
        if s.cracking:
            change = "Cracking: the majority is shrinking"
        elif s.share_week_ago is not None and abs(s.share - s.share_week_ago) >= 0.05:
            change = f"{'More' if s.share > s.share_week_ago else 'Less'} one-sided than a week ago"
        else:
            change = "Steady"
        if s.changed:
            change += f"; {len(s.changed)} changed view"
        note, cids, pr = clean(notes.get(s.key, ""), ids, snap, [str(int(s.share * 100))])
        problems += pr
        rows.append({"debate": s.question, "crowd": crowd, "change": change, "note": note or "–"})
        cited |= cids
    return TableOut("Consensus check", rows, cited, problems)


def deep_dive(llm, settings: Settings, sector_key: str, trigger: str, snap: Snapshot, recent_claims: list[dict],
              words: int) -> Section | None:
    sec = settings.sector(sector_key)
    if not sec:
        return None
    people = settings.people
    chosen = select(recent_claims, people, sectors={sector_key}, tickers=set(sec.watchlist + sec.proxies), limit=90)
    if len(chosen) < 3:
        return None
    user = (f"Moves:\n{snap.sector_tables.get(sector_key, '')}\n\nMaterial from the last few days:\n"
            f"{_claims_block(chosen, people)}")
    return _write(llm, P.DEEP_DIVE.format(sector=sec.name, trigger=trigger, words=words), user,
                  {c["id"]: c for c in chosen}, snap, f"Deep dive: {sec.name}", extra_texts=[trigger],
                  purpose="write_deep_dive")
