"""Write each briefing section from claims and code-built tables (Claude Sonnet)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..analysis.checks import CITE, check_section
from ..analysis.views import DebateState
from ..llm import LLMUnavailable
from ..market.snapshot import Snapshot
from ..settings import Sector, Settings
from ..util import log, truncate
from . import prompts as P


@dataclass
class Section:
    title: str
    markdown: str
    cited: set[int] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)


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


def _write(llm, system: str, user: str, claims_by_id: dict[int, dict], snapshot: Snapshot,
           title: str, extra_texts: list[str] | None = None, purpose: str = "write") -> Section:
    try:
        md = llm.text(tier="write", purpose=purpose, system=system, user=user, max_tokens=2500)
    except LLMUnavailable as e:
        log.warning("Section %s not written: %s", title, e)
        return Section(title, "- Not available today (writing model unavailable).", set(), [str(e)])
    md = md.strip().replace("[m]", "")
    clean, problems = check_section(md, claims_by_id, snapshot.numbers, extra_texts)
    cited = {int(x) for x in CITE.findall(clean)}
    return Section(title, clean, cited, problems)


def _claims_block(claims: list[dict], people: dict) -> str:
    return "\n".join(claim_line(c, people) for c in claims) or "(no claims today)"


def overview(llm, settings: Settings, snap: Snapshot, claims: list[dict], words: int) -> Section:
    people = settings.people
    chosen = select(claims, people, sectors={"macro"}, limit=60)
    asia = [c for c in claims if any(t.endswith((".T", ".HK", ".KS", ".SS")) for t in c.get("tickers") or [])]
    chosen += [c for c in asia if c not in chosen][:10]
    user = (f"Market table (as of {snap.asof_label}):\n{snap.overview_md}\n\nMacro table:\n{snap.macro_md}\n\n"
            f"Claims:\n{_claims_block(chosen, people)}")
    return _write(llm, P.OVERVIEW.format(words=words, asof=snap.asof_label), user,
                  {c["id"]: c for c in chosen}, snap, "Market overview", purpose="write_overview")


def sector_section(llm, settings: Settings, sector: Sector, snap: Snapshot, claims: list[dict],
                   words: int) -> Section:
    people = settings.people
    chosen = select(claims, people, sectors={sector.key}, tickers=set(sector.watchlist), limit=70)
    user = (f"Moves table (already on the page):\n{snap.sector_tables.get(sector.key, '')}\n\n"
            f"Live debates for this sector: {', '.join(sector.debates) or 'none'}\n\n"
            f"Claims:\n{_claims_block(chosen, people)}")
    system = P.SECTOR.format(sector=sector.name, subsectors="; ".join(sector.subsectors) or "all",
                             words=words)
    return _write(llm, system, user, {c["id"]: c for c in chosen}, snap, sector.name,
                  purpose=f"write_{sector.key}")


def elsewhere_section(llm, settings: Settings, snap: Snapshot, claims: list[dict], words: int) -> Section | None:
    trig = [t for t in snap.triggers if not (settings.sector(t.sector) and settings.sector(t.sector).core)]
    if not trig:
        return None
    keys = {t.sector for t in trig}
    people = settings.people
    tick = {t for k in keys if (s := settings.sector(k)) for t in s.watchlist + s.proxies}
    chosen = select(claims, people, sectors=keys | {"elsewhere"}, tickers=tick, limit=40)
    tables = "\n\n".join(snap.sector_tables.get(k, "") for k in keys)
    trig_txt = "\n".join(f"- {settings.sector(t.sector).name if settings.sector(t.sector) else t.sector}: {t.reason}"
                         for t in trig)
    user = f"Triggers:\n{trig_txt}\n\nTables:\n{tables}\n\nClaims:\n{_claims_block(chosen, people)}"
    return _write(llm, P.ELSEWHERE.format(words=words), user, {c["id"]: c for c in chosen}, snap,
                  "Elsewhere", extra_texts=[trig_txt], purpose="write_elsewhere")


def diverge_section(llm, settings: Settings, states: list[DebateState], claims_by_id: dict[int, dict],
                    snap: Snapshot, words: int) -> Section | None:
    states = [s for s in states if s.speakers >= 2][:6]
    if not states:
        return None
    blocks, ids = [], {}
    for s in states:
        lines = [f"Debate {s.key}: {s.question}",
                 f"Counts (weighted): {s.counts}; speakers: {s.speakers}; majority: {s.majority} "
                 f"({int(s.share * 100)}%); crowded: {s.crowded}; cracking: {s.cracking}"]
        for v in s.latest + s.dissenters + s.changed:
            c = claims_by_id.get(v["claim_id"]) if "claim_id" in v else None
            if c:
                ids[c["id"]] = c
                tag = []
                if v.get("priority"):
                    tag.append("priority voice")
                if v.get("contrarian"):
                    tag.append("contrarian")
                if v.get("changed"):
                    tag.append(f"changed view, was {v.get('prev_stance')} on {v.get('prev_date')}")
                lines.append(f"  [c{c['id']}] {v['speaker']} ({v['stance']}{'; ' + ', '.join(tag) if tag else ''}): "
                             f"{c['claim']}")
        blocks.append("\n".join(lines))
    numbers = [f"{int(s.share * 100)}%" for s in states]
    return _write(llm, P.DIVERGE.format(words=words), "\n\n".join(blocks), ids, snap,
                  "Where views diverge", extra_texts=numbers, purpose="write_diverge")


def topline(llm, sections: list[Section], claims_by_id: dict[int, dict], snap: Snapshot) -> Section:
    drafts = "\n\n".join(f"## {s.title}\n{s.markdown}" for s in sections if s.markdown)
    return _write(llm, P.TOPLINE, drafts, claims_by_id, snap, "Top line",
                  extra_texts=[s.markdown for s in sections], purpose="write_topline")


def deep_dive(llm, settings: Settings, sector_key: str, snap: Snapshot, week_claims: list[dict],
              words: int) -> Section | None:
    sec = settings.sector(sector_key)
    if not sec:
        return None
    people = settings.people
    chosen = select(week_claims, people, sectors={sector_key}, tickers=set(sec.watchlist + sec.proxies), limit=90)
    if len(chosen) < 3:
        return None
    user = (f"Moves table:\n{snap.sector_tables.get(sector_key, '')}\n\n"
            f"Claims from this week:\n{_claims_block(chosen, people)}")
    s = _write(llm, P.DEEP_DIVE.format(sector=sec.name, words=words), user,
               {c["id"]: c for c in chosen}, snap, f"Friday deep dive: {sec.name}", purpose="write_deep_dive")
    return s
