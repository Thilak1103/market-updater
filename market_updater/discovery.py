"""Weekly Substack discovery: find free research from writers outside the subscription list.

Methods (public pages only, a few hundred requests a week):
1. Recommendations: each subscribed publication's public /recommendations page.
2. Citations: links to other Substack posts inside posts already collected.
3. Topic search: Substack's public search for watch-list names and live debates.
Candidates' latest free posts are scored by Claude Haiku; the best go to the Notion Discovery
database and, if very relevant, into the next briefing as a "new voice".
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus, urlparse

import feedparser
from bs4 import BeautifulSoup

from .db import DB
from .ingest import Item
from .ingest.collect import store
from .llm import LLMUnavailable
from .settings import Settings
from .util import get_with_retry, html_to_text, log, truncate

POST_RE = re.compile(r"https?://([a-z0-9-]+\.substack\.com|[a-z0-9.-]+)/p/[a-z0-9-]+", re.I)

SCORE_SCHEMA = {
    "type": "object",
    "properties": {"scores": {"type": "array", "items": {"type": "object", "properties": {
        "index": {"type": "integer"}, "relevance": {"type": "number"}, "originality": {"type": "number"},
        "against_consensus": {"type": "number"}, "sectors": {"type": "array", "items": {"type": "string"}},
        "why": {"type": "string"}}, "required": ["index", "relevance", "originality", "against_consensus", "why"]}}},
    "required": ["scores"],
}

SCORE_SYSTEM = """Score free newsletter posts for a contrarian buy-side reader covering: {sectors}.
Live debates: {debates}.
For each post give relevance (0-1, to these sectors and debates), originality (0-1: own data,
model or reporting rather than a rewrite of the news), against_consensus (0-1), sectors (from:
TMT, Autos, Staples and retail, Financials, Macro, Elsewhere) and a one-line why."""


def _origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def recommendations(subscribed: list[str]) -> Counter:
    found: Counter = Counter()
    mine = {_origin(u) for u in subscribed}
    for origin in mine:
        try:
            r = get_with_retry(origin + "/recommendations", tries=1)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.startswith("http") and ("substack.com" in href or "/p/" not in href):
                    o = _origin(href)
                    if o not in mine and "substack.com" in o and o != "https://substack.com":
                        found[o] += 1
        except Exception:
            continue
        time.sleep(1)
    return found


def citations(db: DB, subscribed: list[str], days: int = 7) -> Counter:
    mine = {_origin(u) for u in subscribed}
    since = (datetime.utcnow() - timedelta(days=days)).isoformat(timespec="seconds")
    found: Counter = Counter()
    for row in db.q("SELECT text FROM items WHERE fetched>=? AND source_type IN ('substack','rss','email')", (since,)):
        for m in POST_RE.finditer(row["text"] or ""):
            o = _origin(m.group(0))
            if o not in mine and o != "https://substack.com":
                found[o] += 1
    return found


def topic_search(terms: list[str], per_term: int = 5) -> Counter:
    found: Counter = Counter()
    for t in terms:
        try:
            r = get_with_retry(f"https://substack.com/api/v1/post/search?query={quote_plus(t)}&page=0", tries=1)
            if r.status_code != 200:
                continue
            for res in (r.json().get("results") or [])[:per_term]:
                url = res.get("canonical_url") or ""
                if url:
                    found[_origin(url)] += 1
        except Exception:
            continue
        time.sleep(1)
    return found


def run(settings: Settings, db: DB, llm, notion=None) -> dict:
    cfg = settings.get("substack_discovery", {}) or {}
    subscribed = [s.feed_url for s in settings.sources if s.type == "substack" and s.feed_url]
    known = {_origin(u) for u in subscribed} | {r["pub_url"] for r in db.q("SELECT pub_url FROM discovery")}
    via: dict[str, str] = {}
    pool: Counter = Counter()
    for name, counter in (("Recommendation", recommendations(subscribed)), ("Citation", citations(db, subscribed)),
                          ("Topic search", topic_search(
                              [w for s in settings.core_sectors for w in s.keywords[:2]] + list(settings.debates)[:6]))):
        for o, n in counter.items():
            if o in known:
                continue
            pool[o] += n
            via.setdefault(o, name)
    cands = [o for o, _ in pool.most_common(int(cfg.get("candidates_per_week", 100)) // 2)]
    posts = []
    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    for o in cands:
        try:
            feed = feedparser.parse(get_with_retry(o + "/feed", tries=1).content)
        except Exception:
            continue
        for e in feed.entries[:2]:
            t = e.get("published_parsed")
            if t and datetime.fromtimestamp(time.mktime(t), tz=timezone.utc) < cutoff:
                continue
            html = (e.get("content") or [{}])[0].get("value", "") or e.get("summary", "")
            text = html_to_text(html)
            if len(text) < 800:    # paywalled teaser
                continue
            posts.append({"origin": o, "pub": feed.feed.get("title", o), "url": e.get("link"),
                          "title": e.get("title", ""), "text": text})
        time.sleep(0.5)
    if not posts:
        return {"candidates": len(cands), "scored": 0}
    sectors = ", ".join(s.name for s in settings.core_sectors)
    system = SCORE_SYSTEM.format(sectors=sectors, debates="; ".join(settings.debates.values()))
    scored = []
    for i in range(0, len(posts), 10):
        batch = posts[i:i + 10]
        user = "\n\n".join(f"<post index=\"{j}\" title=\"{p['title']}\">{truncate(p['text'], 6000)}</post>"
                           for j, p in enumerate(batch))
        try:
            res = llm.json(tier="extract", purpose="discovery_score", system=system, user=user,
                           schema=SCORE_SCHEMA, max_tokens=2500)
        except LLMUnavailable:
            break
        for s in res.get("scores", []):
            j = s.get("index")
            if isinstance(j, int) and 0 <= j < len(batch):
                p = batch[j]
                score = 0.5 * s["relevance"] + 0.3 * s["originality"] + 0.2 * s["against_consensus"]
                scored.append({**p, "score": round(score, 2), "why": s.get("why", ""),
                               "sectors": s.get("sectors", [])})
    best: dict[str, dict] = {}
    for p in sorted(scored, key=lambda x: -x["score"]):
        best.setdefault(p["origin"], p)
    shortlist = list(best.values())[: int(cfg.get("weekly_shortlist", 5))]
    today = datetime.utcnow().date().isoformat()
    for p in shortlist:
        db.ex("INSERT OR IGNORE INTO discovery(pub_url,name,first_seen,score,found_via,example_post,why,sectors) "
              "VALUES (?,?,?,?,?,?,?,?)", (p["origin"], p["pub"], today, p["score"], via.get(p["origin"]),
                                          p["url"], p["why"], json.dumps(p["sectors"])))
    # Very relevant free posts go into the next briefing as "new voice" items.
    min_score = float(cfg.get("daily_new_voice_min_score", 0.8))
    store(db, [Item(url=p["url"], title=p["title"], text=p["text"], source_name=f"{p['pub']} (new voice)",
                    source_type="discovery", kind="discovery") for p in scored if p["score"] >= min_score])
    if notion is not None:
        from .publish.pages import push_discovery
        push_discovery(notion, db)
    log.info("Discovery: %d candidates, %d posts scored, %d shortlisted", len(cands), len(scored), len(shortlist))
    return {"candidates": len(cands), "scored": len(scored), "shortlisted": len(shortlist)}
