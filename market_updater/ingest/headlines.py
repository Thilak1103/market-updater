"""Watch-list headlines from Google News RSS (headline and link only)."""
from __future__ import annotations

import time
from datetime import datetime
from urllib.parse import quote_plus

import feedparser

from ..util import get_with_retry
from . import Item
from .rss import _entry_time

GN = "https://news.google.com/rss/search?q={q}&hl=en-GB&gl=GB&ceid=GB:en"


def watchlist_headlines(names: dict[str, str], sector_of: dict[str, str], since: datetime,
                        per_name: int = 4) -> list[Item]:
    """names: ticker -> company name. Returns recent headlines mentioning each company."""
    out: list[Item] = []
    for ticker, name in names.items():
        q = f'"{name}" when:2d' if name and name != ticker else f"{ticker} stock when:2d"
        try:
            r = get_with_retry(GN.format(q=quote_plus(q)), tries=2)
            feed = feedparser.parse(r.content)
        except Exception:
            continue
        n = 0
        for e in feed.entries:
            t = _entry_time(e)
            if t and t < since:
                continue
            src = (e.get("source") or {}).get("title", "Google News")
            title = e.get("title", "")
            out.append(Item(url=e.get("link", ""), title=title,
                            text=f"{title}. ({src}) {e.get('summary', '')[:300]}",
                            source_name=src, source_type="headline", kind="headline",
                            published=t, sectors=[sector_of.get(ticker, "elsewhere")]))
            n += 1
            if n >= per_name:
                break
        time.sleep(0.5)
    return out


def people_mentions(people: list, since: datetime, per_person: int = 3) -> list[Item]:
    """Recent news quoting people who have no feed of their own (sources of type press_mentions)."""
    out: list[Item] = []
    for p in people:
        q = f'"{p.name}"' + (f' "{p.org}"' if p.org else "") + " when:2d"
        try:
            r = get_with_retry(GN.format(q=quote_plus(q)), tries=2)
            feed = feedparser.parse(r.content)
        except Exception:
            continue
        for e in feed.entries[:per_person]:
            t = _entry_time(e)
            if t and t < since:
                continue
            src = (e.get("source") or {}).get("title", "Google News")
            title = e.get("title", "")
            out.append(Item(url=e.get("link", ""), title=title,
                            text=f"{title}. ({src}; mentions {p.name}) {e.get('summary', '')[:300]}",
                            source_name=src, source_type="headline", kind="headline",
                            published=t, sectors=list(p.sectors)))
        time.sleep(0.5)
    return out
