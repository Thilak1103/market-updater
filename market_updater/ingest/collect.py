"""Run every collector, store new items, mark repeats and transcribe priority podcasts."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from ..db import DB
from ..settings import Settings
from ..util import fingerprint, jaccard, log, word_set
from . import Item
from .email_inbox import fetch_inbox
from .headlines import people_mentions, watchlist_headlines
from .rss import fetch_feed
from .transcribe import TranscriptionError, transcribe_url
from .web import fetch_index

MAX_TEXT = 60_000


def lookback(now: datetime) -> datetime:
    """Weekday mornings look back ~30h; Mondays cover the weekend."""
    hours = 78 if now.weekday() == 0 else 30
    return now - timedelta(hours=hours)


def _repeat_of(db: DB, title: str) -> str | None:
    """Return the earlier story title if this headline repeats one from the last three days."""
    cutoff = (datetime.utcnow() - timedelta(days=3)).date().isoformat()
    words = word_set(title)
    if len(words) < 4:
        return None
    for row in db.q("SELECT title, words FROM stories WHERE date>=?", (cutoff,)):
        if jaccard(words, json.loads(row["words"])) >= 0.6:
            return row["title"]
    return None


def store(db: DB, items: list[Item]) -> int:
    n = 0
    for it in items:
        if not it.url or db.seen_url(it.url):
            continue
        fp = fingerprint(it.title, it.text)
        if db.seen_fingerprint(fp):
            continue
        rid = db.add_item(
            url=it.url, fingerprint=fp, source_name=it.source_name, source_type=it.source_type,
            kind=it.kind, title=it.title[:500],
            published=(it.published or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
            text=it.text[:MAX_TEXT], is_subscriber=it.is_subscriber, sectors=it.sectors,
            repeat_of=_repeat_of(db, it.title) if it.kind in ("headline", "article") else None,
        )
        n += 1 if rid else 0
    return n


def _transcribe(db: DB, items: list[Item], every_run: set[str], budget_min: float) -> float:
    used = 0.0
    for it in items:
        if not it.audio_url or (it.source_name not in every_run and it.source_type != "inbox"):
            continue
        if db.one("SELECT 1 FROM transcribed WHERE audio_url=?", (it.audio_url,)):
            continue
        if used >= budget_min:
            log.info("Audio budget reached; %s keeps its show notes", it.title)
            continue
        try:
            text, secs = transcribe_url(it.audio_url)
            it.text = f"{it.text}\n\nTranscript:\n{text}" if it.text else text
            used += secs / 60
            db.ex("INSERT OR IGNORE INTO transcribed(audio_url, done) VALUES (?,?)",
                  (it.audio_url, datetime.utcnow().isoformat(timespec="seconds")))
            db.log_usage("groq", "whisper", int(secs), 0, 0.0, "transcription")
        except TranscriptionError as e:
            log.warning("Transcription skipped for %s: %s", it.title, e)
            if "rate limit" in str(e):
                break
        except Exception as e:
            log.warning("Transcription failed for %s: %s", it.title, e)
    return used


def _resolve_podcast_feeds(settings: Settings, db: DB) -> None:
    """Podcasts listed without a feed URL get one looked up by name (cached for 30 days)."""
    from .rss import podcast_feed_lookup

    for src in settings.sources:
        if src.type != "podcast" or src.feed_url or not src.active:
            continue
        row = db.one("SELECT url, checked FROM feed_lookup WHERE name=?", (src.name,))
        fresh = row and row["checked"] >= (datetime.utcnow() - timedelta(days=30)).isoformat()
        if fresh:
            src.feed_url = row["url"] or None
            continue
        try:
            url = podcast_feed_lookup(src.name)
        except Exception as e:
            log.warning("Podcast lookup failed for %s: %s", src.name, e)
            continue
        db.ex("INSERT OR REPLACE INTO feed_lookup VALUES (?,?,?)",
              (src.name, url or "", datetime.utcnow().isoformat(timespec="seconds")))
        src.feed_url = url
        log.info("Podcast %s -> %s", src.name, url or "not found")


def collect_all(settings: Settings, db: DB, notion=None, now: datetime | None = None,
                company_names: dict[str, str] | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = lookback(now)
    stats = {"sources_ok": 0, "sources_failed": 0, "items": 0, "audio_minutes": 0.0}
    collected: list[Item] = []
    _resolve_podcast_feeds(settings, db)
    for src in settings.sources:
        if not src.fetchable:
            continue
        try:
            if src.type == "web_page":
                items = fetch_index(src, db.seen_url)
            else:
                items = fetch_feed(src, since)
            for it in items:
                it.sectors = it.sectors or src.sectors
            collected += items
            db.source_ok(src.name)
            stats["sources_ok"] += 1
        except Exception as e:
            db.source_error(src.name, str(e))
            stats["sources_failed"] += 1
            log.warning("Source %s failed: %s", src.name, e)

    try:
        collected += fetch_inbox(settings)
    except Exception as e:
        log.warning("Email inbox failed: %s", e)

    if notion is not None:
        try:
            from .notion_inbox import process_inbox
            collected += process_inbox(notion)
        except Exception as e:
            log.warning("Notion inbox failed: %s", e)

    if company_names and os.environ.get("MU_HEADLINES", "1") == "1":
        sector_of = {t: settings.sector_of_ticker(t) or "elsewhere" for t in company_names}
        collected += watchlist_headlines(company_names, sector_of, since)

    quoted = [s for s in settings.sources
              if s.active and (s.type == "press_mentions" or s.also_mentions)]
    if quoted and os.environ.get("MU_HEADLINES", "1") == "1":
        try:
            collected += people_mentions(quoted, since)
        except Exception as e:
            log.warning("People mentions failed: %s", e)

    every_run = {s.name for s in settings.sources if s.transcript == "every_run"}
    budget = float(settings.get("ingest.audio_minutes_per_run", 150))
    stats["audio_minutes"] = _transcribe(db, collected, every_run, budget)
    stats["items"] = store(db, collected)
    log.info("Collected %d new items from %d sources (%d failed)",
             stats["items"], stats["sources_ok"], stats["sources_failed"])
    return stats
