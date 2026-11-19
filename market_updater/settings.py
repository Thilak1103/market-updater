"""Settings: the YAML seed in config/briefing.yaml, overridden by the Notion settings databases.

Notion is the live copy (Sectors, Sources, Debates, Settings). The YAML file is the seed
and the fallback when Notion can't be reached.
"""
from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .util import log

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("MU_CONFIG", ROOT / "config" / "briefing.yaml"))
DATA_DIR = Path(os.environ.get("MU_DATA_DIR", ROOT / "data"))

SECTOR_LABELS = {
    "tmt": "TMT", "autos": "Autos", "staples_retail": "Staples and retail",
    "financials": "Financials", "macro": "Macro", "elsewhere": "Elsewhere",
}
LABEL_TO_KEY = {v.lower(): k for k, v in SECTOR_LABELS.items()}


@dataclass
class Source:
    name: str
    type: str                      # substack, rss, email, web_page, podcast, bluesky, press_mentions, api
    feed_url: str | None = None
    org: str = ""
    sectors: list[str] = field(default_factory=list)   # sector keys
    priority: bool = False
    lean: str = "mixed"
    contrarian: bool = False
    interested: bool = False
    ideas: bool = False
    transcript: str | None = None  # "every_run" | "on_demand" for podcasts
    active: bool = True
    notes: str = ""
    sender_contains: str | None = None   # for email sources

    @property
    def fetchable(self) -> bool:
        return self.active and bool(self.feed_url) and self.type not in ("press_mentions", "email", "api")


@dataclass
class Sector:
    key: str
    name: str
    coverage: str = "core"         # core | elsewhere | off
    order: int = 99
    subsectors: list[str] = field(default_factory=list)
    proxies: list[str] = field(default_factory=list)
    watchlist: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    debates: list[str] = field(default_factory=list)

    @property
    def core(self) -> bool:
        return self.coverage == "core"


@dataclass
class Settings:
    raw: dict[str, Any]
    sectors: list[Sector]
    sources: list[Source]
    debates: dict[str, str]
    loaded_from_notion: bool = False

    # Convenience accessors -------------------------------------------------
    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def core_sectors(self) -> list[Sector]:
        return sorted([s for s in self.sectors if s.core], key=lambda s: s.order)

    @property
    def elsewhere_sectors(self) -> list[Sector]:
        return [s for s in self.sectors if s.coverage == "elsewhere"]

    def sector(self, key: str) -> Sector | None:
        return next((s for s in self.sectors if s.key == key), None)

    @property
    def all_watchlist(self) -> list[str]:
        out: list[str] = []
        for s in self.sectors:
            if s.coverage != "off":
                out += [t for t in s.watchlist if t not in out]
        return out

    def sector_of_ticker(self, ticker: str) -> str | None:
        for s in self.sectors:
            if ticker in s.watchlist or ticker in s.proxies:
                return s.key
        return None

    @property
    def people(self) -> dict[str, Source]:
        """Named people and outlets by lower-case name, for speaker matching."""
        return {s.name.lower(): s for s in self.sources}

    def to_json(self) -> str:
        return json.dumps({
            "sectors": [asdict(s) for s in self.sectors],
            "sources": [asdict(s) for s in self.sources],
            "debates": self.debates,
            "raw": self.raw,
        }, indent=2, default=str)


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------

def _yaml_sources(raw: dict) -> list[Source]:
    out: list[Source] = []
    typemap = {"web_index": "web_page", "podcast_mentions": "press_mentions"}
    for p in raw.get("people", []):
        ch = (p.get("channels") or [{}])[0]
        t = typemap.get(ch.get("type", "rss"), ch.get("type", "rss"))
        url = ch.get("url")
        if t == "substack" and url and not url.endswith("/feed"):
            url = url.rstrip("/") + "/feed"
        out.append(Source(
            name=p["name"], type=t, feed_url=url, org=p.get("org", ""),
            sectors=list(p.get("sectors", [])), priority=bool(p.get("priority")),
            lean=p.get("lean", "mixed"), contrarian=bool(p.get("contrarian")),
            interested=bool(p.get("interested")), notes=p.get("why", "") or p.get("note", ""),
            sender_contains=ch.get("sender_contains"),
        ))
    feeds = raw.get("feeds", {})
    for s in feeds.get("substacks", []):
        url = s.get("url")
        out.append(Source(
            name=s["name"], type="substack", feed_url=(url.rstrip("/") + "/feed") if url else None,
            sectors=list(s.get("sectors", [])), ideas=bool(s.get("ideas")),
            contrarian=bool(s.get("contrarian")), active=bool(s.get("active", True)),
        ))
    for group in ("research", "central_banks", "sector_data", "headlines"):
        for f in feeds.get(group, []):
            if f.get("type") in ("api", "google_news_rss"):
                continue
            out.append(Source(
                name=f["name"], type="web_page" if f.get("type") == "web_index" else "rss",
                feed_url=f.get("url"), sectors=[f["sector"]] if f.get("sector") else ["macro"],
                notes=group,
            ))
    short = feeds.get("short_sellers") or (raw.get("substack_discovery") or {}).get("short_sellers") or []
    for f in short:
        out.append(Source(name=f["name"], type="web_page", feed_url=f.get("url"),
                          sectors=["elsewhere"], contrarian=True, ideas=True, notes="short seller"))
    for h in (feeds.get("bluesky", {}) or {}).get("handles", []) or []:
        out.append(Source(name=h, type="bluesky", feed_url=f"https://bsky.app/profile/{h}/rss"))
    for p in raw.get("podcasts", []):
        out.append(Source(
            name=p["name"], type="podcast", feed_url=p.get("url"),
            sectors=[p["sector"]] if p.get("sector") else [],
            transcript="every_run" if p.get("transcript") == "always" else "on_demand",
        ))
    # de-duplicate by name, keeping the first (people before substacks)
    seen: set[str] = set()
    uniq = []
    for s in out:
        if s.name.lower() in seen:
            continue
        seen.add(s.name.lower())
        uniq.append(s)
    return uniq


def _yaml_sectors(raw: dict) -> list[Sector]:
    out = []
    for i, s in enumerate(raw.get("sectors", [])):
        out.append(Sector(
            key=s["id"], name=s["name"], coverage="core" if s.get("core") else "elsewhere",
            order=s.get("order", 50 + i), subsectors=s.get("subsectors", []),
            proxies=[p["ticker"] for p in s.get("proxies", []) if p.get("ticker")],
            watchlist=list(s.get("watchlist", [])), keywords=list(s.get("keywords", [])),
            debates=list(s.get("debates", [])),
        ))
    return out


def load_yaml(path: Path = CONFIG_PATH) -> Settings:
    raw = yaml.safe_load(path.read_text())
    return Settings(raw=raw, sectors=_yaml_sectors(raw), sources=_yaml_sources(raw),
                    debates=dict(raw.get("debates", {})))


# ---------------------------------------------------------------------------
# Notion overrides
# ---------------------------------------------------------------------------

_SETTING_MAP = {
    "trade ideas per day": ("trade_ideas.per_day", "range"),
    "minimum pair correlation (1y)": ("trade_ideas.min_pair_correlation_1y", "float"),
    "stop (multiple of 60-day spread volatility)": ("trade_ideas.stop_spread_vol_multiple", "float"),
    "maximum holding period (trading days)": ("trade_ideas.max_holding_trading_days", "int"),
    "peers in each comps table": ("trade_ideas.comps.peers_per_idea", "range"),
    "elsewhere: sector move threshold": ("thresholds.elsewhere_sector_move_pct", "float"),
    "elsewhere: single-stock move threshold": ("thresholds.elsewhere_single_stock_move_pct", "float"),
    "earnings surprise threshold": ("thresholds.earnings_surprise_pct", "float"),
    "full earnings reviews per day": ("earnings.full_reviews_per_day", "int"),
    "crowded consensus share": ("thresholds.crowded_consensus_share", "share"),
    "narrative cracking drop": ("thresholds.narrative_cracking_drop", "points"),
    "target length": ("budget.target_words", "range"),
    "claude credit guard": ("models.credit_guard.move_extraction_to_fallback_at_usd", "float"),
    "substack discovery shortlist": ("substack_discovery.weekly_shortlist", "int"),
}


def _set_path(d: dict, path: str, value: Any) -> None:
    parts = path.split(".")
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = value


def _nums(s: str) -> list[float]:
    return [float(x.replace(",", "")) for x in re.findall(r"\d[\d,]*\.?\d*", s or "")]


def parse_setting(kind: str, value: str) -> Any:
    n = _nums(value)
    if not n:
        raise ValueError(f"no number in {value!r}")
    if kind == "int":
        return int(n[0])
    if kind == "float":
        return float(n[0])
    if kind == "range":
        return [int(n[0]), int(n[1] if len(n) > 1 else n[0])]
    if kind == "share":     # "75% with at least 5 speakers"
        return n[0] / 100 if n[0] > 1 else n[0]
    if kind == "points":    # "20 points in a week"
        return n[0] / 100 if n[0] > 1 else n[0]
    raise ValueError(kind)


def merge_sources(notion_sources: list[Source], yaml_sources: list[Source]) -> list[Source]:
    """Notion is the live list; where a Notion row has no feed URL, use the YAML one.

    Rows deleted in Notion stay deleted. Run `sync-sources` once to copy YAML URLs and any
    YAML-only sources into Notion so the table shows everything.
    """
    by_name = {y.name.lower(): y for y in yaml_sources}
    for src in notion_sources:
        y = by_name.get(src.name.lower())
        if y and not src.feed_url and y.feed_url:
            src.feed_url = y.feed_url
            if src.type in ("rss", "web_page", "press_mentions") and y.type != src.type:
                src.type = y.type
        elif y and not src.feed_url and y.type == "press_mentions":
            src.type = "press_mentions"
    return notion_sources


def apply_notion(settings: Settings, notion: Any) -> Settings:
    """Override settings with the Notion databases. Any failure keeps the YAML values."""
    from .publish.notion_settings import read_debates, read_settings_rows, read_sectors, read_sources

    s = copy.deepcopy(settings)
    try:
        sectors = read_sectors(notion, s)
        sources = read_sources(notion)
        debates = read_debates(notion)
        rows = read_settings_rows(notion)
    except Exception as e:  # keep YAML on any Notion problem
        log.warning("Could not read settings from Notion (%s); using config/briefing.yaml", e)
        return settings
    if sectors:
        s.sectors = sectors
    if sources:
        s.sources = merge_sources(sources, settings.sources)
    if debates:
        s.debates = debates
    for name, value in rows.items():
        spec = _SETTING_MAP.get(name.lower().strip())
        if not spec:
            continue
        try:
            _set_path(s.raw, spec[0], parse_setting(spec[1], value))
            if spec[0] == "thresholds.crowded_consensus_share":
                n = _nums(value)
                if len(n) > 1:
                    _set_path(s.raw, "thresholds.crowded_min_speakers", int(n[1]))
        except ValueError:
            log.warning("Ignoring Notion setting %r = %r (no number found)", name, value)
    s.loaded_from_notion = True
    return s


def load(notion: Any | None = None) -> Settings:
    settings = load_yaml()
    if notion is not None:
        settings = apply_notion(settings, notion)
    return settings
