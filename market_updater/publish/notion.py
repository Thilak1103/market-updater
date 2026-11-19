"""Minimal Notion API client (data-source API), with property helpers and rate limiting."""
from __future__ import annotations

import os
import time
from typing import Any, Iterable

import httpx

from ..util import chunks, log

API = "https://api.notion.com/v1"
NOTION_VERSION = os.environ.get("NOTION_VERSION", "2025-09-03")


class Notion:
    def __init__(self, token: str | None = None, ids: dict[str, str] | None = None):
        self.token = token or os.environ.get("NOTION_TOKEN")
        if not self.token:
            raise RuntimeError("NOTION_TOKEN is not set")
        self.ids = ids or {}
        self.client = httpx.Client(timeout=60, headers={
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })
        self._last = 0.0

    # -- transport ------------------------------------------------------------
    def _req(self, method: str, path: str, json: dict | None = None) -> dict:
        for attempt in range(5):
            wait = 0.35 - (time.time() - self._last)     # about 3 requests a second
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            r = self.client.request(method, API + path, json=json)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(float(r.headers.get("Retry-After", 2 * (attempt + 1))))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"Notion {method} {path} -> {r.status_code}: {r.text[:500]}")
            return r.json()
        raise RuntimeError(f"Notion {method} {path} kept failing")

    # -- data sources ---------------------------------------------------------
    def ds(self, name: str) -> str:
        if name not in self.ids:
            raise KeyError(f"No Notion data source id configured for {name!r}")
        return self.ids[name]

    def query(self, ds_name: str, filter: dict | None = None, sorts: list | None = None) -> list[dict]:
        out: list[dict] = []
        body: dict[str, Any] = {"page_size": 100}
        if filter:
            body["filter"] = filter
        if sorts:
            body["sorts"] = sorts
        while True:
            data = self._req("POST", f"/data_sources/{self.ds(ds_name)}/query", body)
            out += data.get("results", [])
            if not data.get("has_more"):
                return out
            body["start_cursor"] = data["next_cursor"]

    def create_page(self, ds_name: str, properties: dict, children: list[dict] | None = None,
                    icon: str | None = None) -> dict:
        children = children or []
        body: dict[str, Any] = {
            "parent": {"type": "data_source_id", "data_source_id": self.ds(ds_name)},
            "properties": properties,
            "children": children[:100],
        }
        if icon:
            body["icon"] = {"type": "emoji", "emoji": icon}
        page = self._req("POST", "/pages", body)
        if len(children) > 100:
            self.append(page["id"], children[100:])
        return page

    def page_text(self, page_id: str, max_blocks: int = 300) -> str:
        """Plain text of a page's body (paragraphs, headings, lists, quotes)."""
        parts, cursor = [], None
        while len(parts) < max_blocks:
            q = f"/blocks/{page_id}/children?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
            data = self._req("GET", q)
            for b in data.get("results", []):
                body = b.get(b.get("type", ""), {})
                text = "".join(t.get("plain_text", "") for t in body.get("rich_text", []))
                if text.strip():
                    parts.append(text)
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return "\n".join(parts)

    def append(self, block_id: str, children: list[dict]) -> None:
        for batch in chunks(children, 100):
            self._req("PATCH", f"/blocks/{block_id}/children", {"children": batch})

    def update_page(self, page_id: str, properties: dict) -> dict:
        return self._req("PATCH", f"/pages/{page_id}", {"properties": properties})

    def archive_children(self, page_id: str) -> None:
        """Remove a page's blocks (used when re-publishing the same day's page)."""
        # Always re-read the first page: deleting while following a cursor can skip blocks.
        for _ in range(50):
            data = self._req("GET", f"/blocks/{page_id}/children?page_size=100")
            results = data.get("results", [])
            if not results:
                return
            for b in results:
                self._req("DELETE", f"/blocks/{b['id']}")


# -- property builders --------------------------------------------------------

def _rt(text: str) -> list[dict]:
    text = text or ""
    return [{"type": "text", "text": {"content": c}} for c in [text[i:i + 2000] for i in range(0, len(text), 2000)]] or []


def title(text: str) -> dict:
    return {"title": _rt(text)}


def rich(text: str) -> dict:
    return {"rich_text": _rt(text)}


def select(name: str | None) -> dict:
    return {"select": {"name": name} if name else None}


def multi(names: Iterable[str]) -> dict:
    return {"multi_select": [{"name": n} for n in names if n]}


def checkbox(v: bool) -> dict:
    return {"checkbox": bool(v)}


def number(v: float | None) -> dict:
    return {"number": None if v is None else float(v)}


def url(u: str | None) -> dict:
    return {"url": u or None}


def date(iso: str | None) -> dict:
    return {"date": {"start": iso} if iso else None}


# -- property readers ---------------------------------------------------------

def prop(page: dict, name: str) -> Any:
    p = page.get("properties", {}).get(name)
    if not p:
        return None
    t = p.get("type")
    v = p.get(t)
    if t in ("title", "rich_text"):
        return "".join(x.get("plain_text", "") for x in v or [])
    if t == "select":
        return v.get("name") if v else None
    if t == "multi_select":
        return [x["name"] for x in v or []]
    if t in ("checkbox", "number", "url", "email"):
        return v
    if t == "date":
        return v.get("start") if v else None
    if t in ("created_time", "last_edited_time"):
        return v
    return v


def ids_from_settings(settings) -> dict[str, str]:
    return dict(settings.get("notion.data_sources", {}) or {})


def connect(settings) -> Notion | None:
    """A Notion client if a token is configured, else None (YAML-only mode)."""
    if not os.environ.get("NOTION_TOKEN"):
        log.info("NOTION_TOKEN not set: running without Notion")
        return None
    return Notion(ids=ids_from_settings(settings))
