"""Stance tracking, changed views, and consensus / divergence detection per debate."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..db import DB
from ..settings import Settings


def _match_person(settings: Settings, speaker: str, via: str | None):
    people = settings.people
    for name in (speaker, via):
        if name and name.lower() in people:
            return people[name.lower()]
    sp = (speaker or "").lower()
    for key, src in people.items():
        if len(key) > 4 and (key in sp or sp in key) and sp:
            return src
    return None


def record_views(settings: Settings, db: DB, since: datetime) -> list[dict]:
    """Store each new opinion/forecast with a debate and stance; flag changed positions."""
    new_views = []
    for c in db.claims_since(since):
        if c["kind"] not in ("opinion", "forecast", "position") or not c["stance"]:
            continue
        debate = c["debate"] or ""
        if not debate or debate.startswith("new:") or debate not in settings.debates:
            continue
        if db.one("SELECT 1 FROM views WHERE claim_id=?", (c["id"],)):
            continue
        person = _match_person(settings, c["speaker"], c["via"])
        prev = db.one("SELECT stance, date, url FROM views WHERE lower(speaker)=lower(?) AND debate=? "
                      "ORDER BY date DESC, id DESC LIMIT 1", (c["speaker"], debate))
        changed = bool(prev and prev["stance"] != c["stance"])
        day = (c["published"] or c["created"])[:10]
        vid = db.ex(
            "INSERT INTO views(claim_id,speaker,debate,stance,date,url,priority,contrarian,changed,"
            "prev_stance,prev_date,prev_url) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (c["id"], c["speaker"], debate, c["stance"], day, c["url"],
             int(bool(person and person.priority)), int(bool(person and person.contrarian)),
             int(changed), prev["stance"] if prev else None, prev["date"] if prev else None,
             prev["url"] if prev else None))
        new_views.append({"id": vid, "speaker": c["speaker"], "debate": debate, "stance": c["stance"],
                          "changed": changed, "prev": dict(prev) if prev else None,
                          "claim": c["claim"], "url": c["url"],
                          "priority": bool(person and person.priority)})
    return new_views


@dataclass
class DebateState:
    key: str
    question: str
    counts: dict[str, float]
    speakers: int
    majority: str | None
    share: float
    crowded: bool
    cracking: bool
    share_week_ago: float | None
    dissenters: list[dict] = field(default_factory=list)
    changed: list[dict] = field(default_factory=list)
    latest: list[dict] = field(default_factory=list)


def _latest_stances(db: DB, debate: str, start: str, end: str) -> dict[str, dict]:
    rows = db.q("SELECT v.*, c.claim FROM views v JOIN claims c ON c.id=v.claim_id WHERE v.debate=? "
                "AND v.date>=? AND v.date<=? ORDER BY v.date, v.id", (debate, start, end))
    latest: dict[str, dict] = {}
    for r in rows:
        latest[r["speaker"].lower()] = dict(r)
    return latest


def _shares(latest: dict[str, dict], priority_weight: float) -> tuple[Counter, float, str | None]:
    counts: Counter = Counter()
    for v in latest.values():
        counts[v["stance"]] += priority_weight if v["priority"] else 1.0
    total = sum(counts[s] for s in ("bull", "bear"))
    if not total:
        return counts, 0.0, None
    majority = "bull" if counts["bull"] >= counts["bear"] else "bear"
    return counts, counts[majority] / total, majority


def debate_states(settings: Settings, db: DB, day: date) -> list[DebateState]:
    th = settings.get("thresholds", {}) or {}
    window = int(th.get("consensus_window_trading_days", 10)) * 7 // 5
    crowd_share = float(th.get("crowded_consensus_share", 0.75))
    crowd_min = int(th.get("crowded_min_speakers", 5))
    crack = float(th.get("narrative_cracking_drop", 0.20))
    pw = float(th.get("priority_voice_weight", 2.0))
    out = []
    for key, question in settings.debates.items():
        start = (day - timedelta(days=window)).isoformat()
        latest = _latest_stances(db, key, start, day.isoformat())
        if not latest:
            continue
        counts, share, majority = _shares(latest, pw)
        wk_end = day - timedelta(days=7)
        prev_latest = _latest_stances(db, key, (wk_end - timedelta(days=window)).isoformat(), wk_end.isoformat())
        _, prev_share, prev_major = _shares(prev_latest, pw) if prev_latest else (None, None, None)
        crowded = share >= crowd_share and len(latest) >= crowd_min
        cracking = bool(prev_share is not None and prev_major == majority and prev_share - share >= crack)
        dissent = [v for v in latest.values()
                   if majority and v["stance"] in ("bull", "bear") and v["stance"] != majority
                   and (v["priority"] or v["contrarian"])]
        changed = [v for v in latest.values() if v["changed"] and v["date"] >= (day - timedelta(days=3)).isoformat()]
        out.append(DebateState(key, question, dict(counts), len(latest), majority, round(share, 2),
                               crowded, cracking, prev_share, dissent, changed,
                               sorted(latest.values(), key=lambda v: v["date"], reverse=True)[:8]))
    out.sort(key=lambda d: (d.crowded and bool(d.dissenters), d.cracking, bool(d.changed), d.speakers), reverse=True)
    return out


def proposed_debates(db: DB, since: datetime, min_mentions: int = 2) -> list[str]:
    rows = db.q("SELECT debate FROM claims WHERE debate LIKE 'new:%' AND created>=?",
                (since.isoformat(timespec="seconds"),))
    counts = Counter(r["debate"][4:].strip() for r in rows if r["debate"][4:].strip())
    return [k for k, n in counts.items() if n >= min_mentions]
