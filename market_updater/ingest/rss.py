"""RSS/Atom collector for Substacks, blogs, central banks, Bluesky profiles and podcast feeds."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import feedparser

from ..settings import Source
from ..util import get_with_retry, html_to_text, log
from . import Item

MIN_FULL_TEXT = 600   # below this, fetch the article page for the full text


def _entry_time(e) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return datetime.fromtimestamp(time.mktime(t), tz=timezone.utc)
    return None


def _entry_html(e) -> str:
    if e.get("content"):
        return max((c.get("value", "") for c in e["content"]), key=len)
    return e.get("summary", "") or e.get("description", "")


def _audio(e) -> str | None:
    for enc in e.get("enclosures", []) or []:
        if (enc.get("type") or "").startswith("audio") or (enc.get("href") or "").split("?")[0].endswith((".mp3", ".m4a")):
            return enc.get("href")
    for l in e.get("links", []) or []:
        if (l.get("type") or "").startswith("audio"):
            return l.get("href")
    return None


def fetch_feed(source: Source, since: datetime, max_items: int = 8,
               fetch_full: bool = True) -> list[Item]:
    r = get_with_retry(source.feed_url)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"Unreadable feed: {feed.bozo_exception}")
    out: list[Item] = []
    for e in feed.entries[: max_items * 3]:
        published = _entry_time(e)
        if published and published < since:
            continue
        link = e.get("link") or ""
        if not link:
            continue
        html = _entry_html(e)
        text = html_to_text(html)
        audio = _audio(e) if source.type == "podcast" else None
        if fetch_full and not audio and len(text) < MIN_FULL_TEXT and source.type != "bluesky":
            try:
                page = get_with_retry(link)
                if page.status_code == 200:
                    full = html_to_text(page.text)
                    if len(full) > len(text):
                        text = full
            except Exception as ex:  # keep the summary if the page can't be read
                log.debug("Full-text fetch failed for %s: %s", link, ex)
        out.append(Item(
            url=link, title=e.get("title", "").strip() or link, text=text,
            source_name=source.name, source_type=source.type,
            kind="podcast" if source.type == "podcast" else "article",
            published=published, sectors=source.sectors, audio_url=audio,
        ))
        if len(out) >= max_items:
            break
    return out


def discover_feed(page_url: str) -> str | None:
    """Find an RSS/Atom feed advertised on a web page (used when adding sources)."""
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin

    r = get_with_retry(page_url)
    if r.status_code != 200:
        return None
    ctype = r.headers.get("content-type", "")
    if "xml" in ctype or r.text.lstrip().startswith("<?xml"):
        return page_url
    soup = BeautifulSoup(r.text, "lxml")
    for link in soup.find_all("link", attrs={"rel": "alternate"}):
        if "rss" in (link.get("type") or "") or "atom" in (link.get("type") or ""):
            return urljoin(page_url, link.get("href"))
    for guess in ("feed", "rss", "feed.xml", "rss.xml", "index.xml"):
        try:
            g = get_with_retry(urljoin(page_url.rstrip("/") + "/", guess), tries=1)
            if g.status_code == 200 and feedparser.parse(g.content).entries:
                return str(g.url)
        except Exception:
            continue
    return None


def podcast_feed_lookup(name: str) -> str | None:
    """Find a podcast's public RSS feed by name through Apple's free podcast search API."""
    from ..util import word_set, jaccard

    r = get_with_retry("https://itunes.apple.com/search",
                       params={"term": name, "media": "podcast", "entity": "podcast", "limit": 5})
    if r.status_code != 200:
        return None
    want = word_set(name)
    best, score = None, 0.0
    for res in r.json().get("results", []):
        s = jaccard(want, word_set(res.get("collectionName", "")))
        if res.get("feedUrl") and s > score:
            best, score = res["feedUrl"], s
    return best if score >= 0.3 else None
