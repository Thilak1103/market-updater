"""Collector for research pages with no RSS feed (e.g. Citadel Securities insights).

Reads the index page, finds new article links on the same site, and extracts their text.
Kept deliberately polite: a few pages per source per run, one second apart.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..settings import Source
from ..util import get_with_retry, html_to_text
from . import Item

SKIP = re.compile(r"/(about|contact|login|signin|sign-in|privacy|terms|legal|careers|jobs|tag|tags|"
                  r"category|categories|search|cookie|subscribe|events|people|team|press-contacts)(/|$)", re.I)


def _site(u: str) -> str:
    host = urlparse(u).netloc.lower()
    return ".".join(host.split(".")[-2:])


def article_links(index_url: str, html: str, limit: int = 15) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    site = _site(index_url)
    base_path = urlparse(index_url).path.rstrip("/")
    seen, out = set(), []
    for a in soup.find_all("a", href=True):
        href = urljoin(index_url, a["href"]).split("#")[0]
        p = urlparse(href)
        if p.scheme not in ("http", "https") or _site(href) != site:
            continue
        path = p.path.rstrip("/")
        if path == base_path or path.count("/") < 2 or SKIP.search(path) or href in seen:
            continue
        if path.lower().endswith((".jpg", ".png", ".svg", ".css", ".js")):
            continue
        text = " ".join(a.get_text(" ").split())
        if len(text) < 20:
            continue
        seen.add(href)
        out.append((href, text))
        if len(out) >= limit:
            break
    return out


def fetch_index(source: Source, seen: callable, max_new: int = 3) -> list[Item]:
    r = get_with_retry(source.feed_url)
    r.raise_for_status()
    out = []
    for href, anchor in article_links(source.feed_url, r.text):
        if seen(href):
            continue
        time.sleep(1.0)
        page = get_with_retry(href)
        if page.status_code != 200:
            continue
        text = html_to_text(page.text)
        if len(text) < 400:
            continue
        title = anchor
        m = re.search(r"<title[^>]*>(.*?)</title>", page.text, re.S | re.I)
        if m:
            title = " ".join(m.group(1).split())[:200]
        out.append(Item(url=href, title=title, text=text, source_name=source.name,
                        source_type="web_page", published=datetime.now(timezone.utc),
                        sectors=source.sectors))
        if len(out) >= max_new:
            break
    return out
