"""Stocks the reader follows: the price, and the news and views that bear on each one.

The list comes from the "Stocks I follow" row in the Notion Settings table (or following.stocks
in briefing.yaml). Each name gets a price line and a few cited bullets on what is moving it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..market.prices import Prices
from ..market.snapshot import Snapshot
from ..settings import Settings
from . import prompts as P
from .sections import _json, claim_line, clean, select

FOLLOWED = P.STYLE + """
The reader follows {name} ({ticker}) closely. Using only the material given, return JSON:
- bullets: two to five, best first. Each has a short "lead" (a few words) and a "text" of one or two
  sentences: what moved the stock and why, the news that bears on it (results, deals, regulation,
  lock-ups, index inclusion, analyst initiations and target changes, competitors), and the strongest
  bull and bear arguments from named people, each with its citation [cN]. Prefer the newest material.
  Never repeat the price line; the reader has it. If nothing new bears on the stock, return one bullet
  saying so plainly.
"""

FOLLOWED_SCHEMA = {
    "type": "object",
    "properties": {"bullets": {"type": "array", "items": {"type": "object", "properties": {
        "lead": {"type": "string"}, "text": {"type": "string"}}, "required": ["lead", "text"]}}},
    "required": ["bullets"],
}


@dataclass
class Followed:
    ticker: str
    name: str
    price_line: str
    bullets: list[str] = field(default_factory=list)
    cited: set[int] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)


def followed_tickers(settings: Settings) -> list[str]:
    return [t.strip().upper() for t in (settings.get("following.stocks") or []) if str(t).strip()]


def _price_line(prices: Prices, ticker: str) -> str:
    q = prices.quote(ticker)
    if not q:
        return "No price yet."
    s = prices.frame[ticker].dropna()
    bits = [f"Last {q.last:,.2f} ({q.change_pct:+.1f}% on the day)"]
    if len(s) > 21:
        bits.append(f"{(s.iloc[-1] / s.iloc[-22] - 1) * 100:+.1f}% over a month")
    bits.append(f"{(s.iloc[-1] / s.iloc[0] - 1) * 100:+.1f}% since {s.index[0].strftime('%-d %b %Y')}")
    return ", ".join(bits) + "."


def _mentions(c: dict, ticker: str, name: str) -> bool:
    if ticker in set(c.get("tickers") or []):
        return True
    first = re.escape(name.split(",")[0].split(" ")[0])
    text = f"{c.get('claim', '')} {c.get('item_title', '')}"
    return bool(first and len(first) > 3 and re.search(rf"\b{first}\b", text, re.I))


def write(llm, settings: Settings, prices: Prices, snap: Snapshot, library: list[dict],
          names: dict[str, str]) -> list[Followed]:
    out = []
    for ticker in followed_tickers(settings):
        name = names.get(ticker) or ticker
        price = _price_line(prices, ticker)
        f = Followed(ticker, name, price)
        about = [c for c in library if _mentions(c, ticker, name)]
        chosen = select(about, settings.people, limit=40)
        if not chosen:
            f.bullets = ["Nothing new in the sources on this name since yesterday."]
            out.append(f)
            continue
        user = (f"Price: {price}\n\nMaterial (newest last):\n" +
                "\n".join(claim_line(c, settings.people) for c in sorted(chosen, key=lambda c: c["id"])))
        res = _json(llm, system=FOLLOWED.format(name=name, ticker=ticker), user=user,
                    schema=FOLLOWED_SCHEMA, purpose="write_followed", max_tokens=2000) or {}
        by_id = {c["id"]: c for c in chosen}
        for b in (res.get("bullets") or [])[:5]:
            text, ids, pr = clean(b.get("text", ""), by_id, snap, [price])
            f.problems += pr
            if text and b.get("lead"):
                f.bullets.append(f"- **{b['lead'].strip().rstrip('.:')}:** {text}")
                f.cited |= ids
        out.append(f)
    return out


def markdown(items: list[Followed]) -> str:
    parts = []
    for f in items:
        parts += [f"### {f.name} ({f.ticker})", f"*{f.price_line}*"]
        parts += [b if b.startswith("- ") else f"- {b}" for b in f.bullets]
    return "\n".join(parts)
