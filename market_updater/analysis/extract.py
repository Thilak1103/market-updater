"""Turn collected items into typed, attributed claims (Claude Haiku).

Each claim must quote a short exact span of its source; claims whose quote can't be found in
the source text are dropped. Speakers are the people who hold a view, not the outlet.
"""
from __future__ import annotations

import json
from typing import Any

from ..db import DB
from ..llm import LLMUnavailable
from ..settings import Settings
from ..util import log, normalise_for_match, truncate

KINDS = ["fact", "data", "opinion", "forecast", "position"]
STANCES = ["bull", "bear", "neutral"]

CLAIM_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": "integer", "description": "Index of the source item, e.g. 0"},
                    "kind": {"type": "string", "enum": KINDS},
                    "speaker": {"type": "string", "description": "Person or institution holding the view; for facts, the publisher"},
                    "via": {"type": "string", "description": "Outlet that carried it, if different from the speaker"},
                    "sector": {"type": "string"},
                    "tickers": {"type": "array", "items": {"type": "string"}},
                    "debate": {"type": "string", "description": "A debate key from the list, 'new:<short-key>' for a new one, or ''"},
                    "stance": {"type": "string", "enum": STANCES + [""]},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "claim": {"type": "string", "description": "One sentence, in your own words"},
                    "evidence_quote": {"type": "string", "description": "A short exact span (5-25 words) copied from the item"},
                    "numbers": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["item", "kind", "speaker", "sector", "claim", "evidence_quote"],
            },
        }
    },
    "required": ["claims"],
}

SYSTEM = """You extract claims from financial news, research, newsletters and podcast transcripts for a
buy-side briefing. Be precise and conservative.

Rules:
- kind: fact (something happened), data (a released number), opinion (a view or argument),
  forecast (a dated prediction), position (a disclosed trade or holding).
- speaker: the person or institution who holds the view. If a newsletter quotes an analyst, the
  analyst is the speaker and the newsletter goes in "via". For plain news facts, the speaker is
  the publisher. Never invent a name.
- Only extract claims that matter to markets: macro, central banks, sectors, companies, flows,
  positioning, valuation, earnings. Skip marketing, housekeeping and generic advice.
- sector: one of {sectors}. Use "macro" for economy-wide, "elsewhere" for other sectors.
- tickers: Yahoo-style tickers if clear (e.g. TSCO.L, NVDA, 7203.T), else [].
- debate: tag opinions and forecasts with the best matching key from this list, or
  "new:<short-key>" if it's clearly a different live debate, or "" if none:
{debates}
- stance: bull/bear/neutral relative to the debate's question or the named asset.
- evidence_quote: copy 5-25 consecutive words EXACTLY from the item text. No paraphrase.
- numbers: every number your claim sentence contains, as written (e.g. "4.2%", "$1.3bn").
- At most {max_claims} claims per item; prefer the ones a portfolio manager would care about.
"""


def _system(settings: Settings, max_claims: int) -> str:
    sectors = ", ".join([s.key for s in settings.sectors if s.coverage != "off"] + ["macro", "elsewhere"])
    debates = "\n".join(f"  {k}: {v}" for k, v in settings.debates.items())
    return SYSTEM.format(sectors=sectors, debates=debates, max_claims=max_claims)


def quote_ok(quote: str, text: str) -> bool:
    q = normalise_for_match(quote)
    return len(q.split()) >= 3 and q in normalise_for_match(text)


def _batches(items: list, max_chars: int = 45_000, max_items: int = 6):
    batch, size = [], 0
    for it in items:
        n = min(len(it["text"] or ""), 40_000)
        if batch and (size + n > max_chars or len(batch) >= max_items):
            yield batch
            batch, size = [], 0
        batch.append(it)
        size += n
    if batch:
        yield batch


def extract_pending(settings: Settings, db: DB, llm, max_claims: int = 10) -> dict:
    items = [dict(r) for r in db.pending_items()]
    stats = {"items": len(items), "claims": 0, "dropped_quotes": 0, "failed_batches": 0}
    system = _system(settings, max_claims)
    for batch in _batches(items):
        parts = []
        for i, it in enumerate(batch):
            parts.append(f"<item index=\"{i}\" source=\"{it['source_name']}\" type=\"{it['source_type']}\" "
                         f"title=\"{it['title']}\" published=\"{it['published']}\">\n"
                         f"{truncate(it['text'] or it['title'], 40_000)}\n</item>")
        try:
            res = llm.json(tier="extract", purpose="extract_claims", system=system,
                           user="\n\n".join(parts), schema=CLAIM_SCHEMA, max_tokens=6000)
        except (LLMUnavailable, Exception) as e:
            log.warning("Extraction failed for a batch of %d items: %s", len(batch), e)
            stats["failed_batches"] += 1
            continue
        for c in res.get("claims", []):
            idx = c.get("item")
            if not isinstance(idx, int) or not 0 <= idx < len(batch):
                continue
            it = batch[idx]
            if not quote_ok(c.get("evidence_quote", ""), it["text"] or it["title"]):
                stats["dropped_quotes"] += 1
                continue
            if c.get("kind") not in KINDS:
                continue
            db.add_claim(it["id"], c)
            stats["claims"] += 1
        with db.tx() as conn:
            conn.executemany("UPDATE items SET extracted=1 WHERE id=?", [(it["id"],) for it in batch])
    log.info("Extracted %d claims from %d items (%d dropped for unverifiable quotes)",
             stats["claims"], stats["items"], stats["dropped_quotes"])
    return stats


def claim_dict(row: Any) -> dict:
    d = dict(row)
    for k in ("tickers", "numbers"):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                d[k] = []
    return d


EPISODE_SYSTEM = """You summarise one long podcast episode, video or research piece for a buy-side reader who
will not listen to it. Return JSON:
- who: the host and guests with their roles, e.g. "Scott Galloway (host) with Aswath Damodaran (NYU Stern)".
- points: 4 to 7 bullets, each one specific argument, forecast or piece of evidence, attributed to the
  person who made it, with any numbers they gave, e.g. "Damodaran: Nvidia's price implies $X of revenue
  by 2030, which needs ...". Skip small talk, ads and anything that only repeats market moves or
  headlines the reader already knows.
- worth_it: one sentence on whether it is worth the reader's full time and which part.
Use only what is in the text. Never invent numbers."""

EPISODE_SCHEMA = {
    "type": "object",
    "properties": {"who": {"type": "string"}, "points": {"type": "array", "items": {"type": "string"}},
                   "worth_it": {"type": "string"}},
    "required": ["who", "points", "worth_it"],
}


def summarise_long_items(db: DB, llm, limit: int = 12) -> int:
    """Write a point-by-point summary of new podcast episodes and long research pieces."""
    rows = db.q("SELECT id, title, source_name, kind, source_type, text FROM items WHERE summary IS NULL "
                "AND ((kind='podcast' AND length(text) > 8000) OR (source_type IN ('substack','web_page','email') "
                "AND length(text) > 15000) OR (source_type='inbox' AND length(text) > 1500)) "
                "ORDER BY (source_type='inbox') DESC, published DESC LIMIT ?", (limit,))
    n = 0
    for r in rows:
        try:
            res = llm.json(tier="extract", purpose="episode_summary", system=EPISODE_SYSTEM,
                           user=f"{r['source_name']}: {r['title']}\n\n{truncate(r['text'], 120_000)}",
                           schema=EPISODE_SCHEMA, max_tokens=1500)
        except Exception as e:
            log.warning("Summary failed for %s: %s", r["title"], e)
            continue
        db.ex("UPDATE items SET summary=? WHERE id=?", (json.dumps(res, ensure_ascii=False), r["id"]))
        n += 1
    if rows:
        log.info("Summarised %d long episodes and pieces", n)
    return n
