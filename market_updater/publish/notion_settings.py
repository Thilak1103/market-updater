"""Read the four Notion settings databases into Settings objects."""
from __future__ import annotations

import re

from ..settings import LABEL_TO_KEY, Sector, Settings, Source
from .notion import Notion, prop

TYPE_MAP = {
    "substack": "substack", "rss": "rss", "email": "email", "web page": "web_page",
    "podcast": "podcast", "youtube": "youtube", "bluesky": "bluesky", "press mentions": "press_mentions", "api": "api",
}
COVERAGE_MAP = {"core": "core", "elsewhere only": "elsewhere", "off": "off"}


def _list(s: str | None, sep: str = ",") -> list[str]:
    return [x.strip() for x in (s or "").split(sep) if x.strip()]


def parse_tickers(s: str | None) -> list[str]:
    """'SMH, XLK, EXV3.DE (check), STOXX Europe 600 Retail tracker (to find)' -> ['SMH','XLK','EXV3.DE']"""
    out = []
    for piece in _list(s):
        if "to find" in piece.lower():
            continue
        tok = piece.split()[0]
        if re.fullmatch(r"[\^A-Z0-9][A-Z0-9.=\-^]*", tok) and len(tok) >= 2:
            out.append(tok)
    return out


def _sector_keys(labels: list[str] | None) -> list[str]:
    return [LABEL_TO_KEY.get(l.lower(), l.lower()) for l in labels or []]


def read_sectors(notion: Notion, settings: Settings) -> list[Sector]:
    rows = notion.query("sectors")
    yaml_by_key = {s.key: s for s in settings.sectors}
    debates_by_sector: dict[str, list[str]] = {}
    for d in notion.query("debates"):
        if prop(d, "Active") and prop(d, "Key"):
            debates_by_sector.setdefault(LABEL_TO_KEY.get((prop(d, "Sector") or "").lower(), "macro"),
                                         []).append(prop(d, "Key"))
    out = []
    for r in rows:
        key = (prop(r, "Key") or "").strip() or re.sub(r"\W+", "_", (prop(r, "Sector") or "").lower())
        base = yaml_by_key.get(key)
        out.append(Sector(
            key=key, name=prop(r, "Sector") or key,
            coverage=COVERAGE_MAP.get((prop(r, "Coverage") or "core").lower(), "core"),
            order=int(prop(r, "Order") or 99),
            subsectors=_list(prop(r, "Subsectors"), ";"),
            proxies=parse_tickers(prop(r, "Proxies")),
            watchlist=parse_tickers(prop(r, "Watch list")),
            keywords=_list(prop(r, "Keywords")),
            debates=debates_by_sector.get(key, base.debates if base else []),
        ))
    return out


def read_sources(notion: Notion) -> list[Source]:
    out = []
    for r in notion.query("sources"):
        name = prop(r, "Source")
        if not name:
            continue
        transcript = prop(r, "Transcript")
        out.append(Source(
            name=name, type=TYPE_MAP.get((prop(r, "Type") or "rss").lower(), "rss"),
            feed_url=prop(r, "Feed URL"), org=prop(r, "Organisation") or "",
            sectors=_sector_keys(prop(r, "Sectors")), priority=bool(prop(r, "Priority")),
            lean=(prop(r, "Lean") or "Mixed").lower(), contrarian=bool(prop(r, "Contrarian")),
            interested=bool(prop(r, "Interested party")), ideas=bool(prop(r, "Feeds trade ideas")),
            transcript={"Every run": "every_run", "On demand": "on_demand"}.get(transcript or ""),
            active=bool(prop(r, "Active")), notes=prop(r, "Notes") or "",
        ))
    return out


def read_debates(notion: Notion) -> dict[str, str]:
    return {prop(d, "Key"): prop(d, "Question") for d in notion.query("debates")
            if prop(d, "Active") and prop(d, "Key")}


def read_settings_rows(notion: Notion) -> dict[str, str]:
    return {prop(r, "Setting"): prop(r, "Value") or "" for r in notion.query("settings") if prop(r, "Setting")}
