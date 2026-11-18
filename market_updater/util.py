"""Small shared helpers: time, HTTP, text cleaning, fingerprints."""
from __future__ import annotations

import hashlib
import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import httpx

LONDON = ZoneInfo("Europe/London")
USER_AGENT = "MarketUpdater/1.0 (personal research briefing; contact via repository owner)"

log = logging.getLogger("market_updater")


def now_london() -> datetime:
    return datetime.now(LONDON)


def today_london() -> date:
    return now_london().date()


def previous_weekday(d: date) -> date:
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def is_friday(d: date) -> bool:
    return d.weekday() == 4


_client: httpx.Client | None = None


def http() -> httpx.Client:
    """One shared HTTP client with sane timeouts and a clear user agent."""
    global _client
    if _client is None:
        _client = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
    return _client


def get_with_retry(url: str, *, params: dict | None = None, headers: dict | None = None,
                   tries: int = 3, backoff: float = 2.0) -> httpx.Response:
    """GET with a small retry on network errors and 429/5xx."""
    last: Exception | None = None
    for attempt in range(tries):
        try:
            r = http().get(url, params=params, headers=headers)
            if r.status_code == 429 or r.status_code >= 500:
                wait = float(r.headers.get("Retry-After", backoff * (attempt + 1)))
                time.sleep(min(wait, 30))
                last = httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                continue
            return r
        except httpx.HTTPError as e:  # network-level failure
            last = e
            time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"GET failed after {tries} tries: {url}: {last}")


_ws = re.compile(r"\s+")


def clean_text(s: str | None) -> str:
    return _ws.sub(" ", s or "").strip()


def normalise_for_match(s: str) -> str:
    """Lowercase, unify quotes/dashes and collapse whitespace for quote checks."""
    s = (s or "").lower()
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace(" ", " ")
    return _ws.sub(" ", s).strip()


def fingerprint(title: str, body: str) -> str:
    base = normalise_for_match(title) + "|" + normalise_for_match(body)[:300]
    return hashlib.sha1(base.encode()).hexdigest()


def word_set(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) > 2}


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    a, b = set(a), set(b)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def html_to_text(html: str) -> str:
    """Article text from HTML, falling back to plain tag stripping."""
    if not html:
        return ""
    try:
        import trafilatura

        out = trafilatura.extract(html, include_comments=False, include_tables=True)
        if out and len(out) > 200:
            return out
    except Exception:  # pragma: no cover - trafilatura edge cases
        pass
    from bs4 import BeautifulSoup

    return clean_text(BeautifulSoup(html, "lxml").get_text(" "))


def chunks(seq: list[Any], n: int) -> Iterable[list[Any]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"
