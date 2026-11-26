"""Things Thilak saved himself (Notion Inbox rows: videos, posts, articles, audio).

Anything he saves is guaranteed a place in the next briefing, with the point-by-point summary
written for long pieces, instead of competing with every other source for a mention.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta


def saved_items(db, now: datetime, hours: int = 30) -> list[dict]:
    rows = db.items_since(now - timedelta(hours=hours))
    out, seen = [], set()
    for r in rows:
        d = dict(r)
        if d.get("source_type") != "inbox" or d.get("url") in seen:
            continue
        seen.add(d.get("url"))
        out.append(d)
    return out


def _summary(d: dict) -> dict | None:
    raw = d.get("summary")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def markdown(items: list[dict]) -> str:
    blocks = []
    for d in items:
        url = d.get("url") or ""
        title = d.get("title") or "Saved link"
        head = f"**[{title}]({url})**" if url.startswith("http") else f"**{title}**"
        src = d.get("source_name") or ""
        if src and src != "Inbox":
            head += f" · {src}"
        s = _summary(d)
        lines = [head]
        if s:
            if s.get("who"):
                lines.append(f"*{s['who']}*")
            lines += [f"- {p}" for p in (s.get("points") or [])[:7]]
            if s.get("worth_it"):
                lines.append(f"**Worth your time?** {s['worth_it']}")
        else:
            text = " ".join((d.get("text") or "").split())
            lines.append(text[:400] + ("…" if len(text) > 400 else ""))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
