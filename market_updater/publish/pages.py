"""Write the briefing and its side tables (Views, Ideas, Discovery, Debates) to Notion."""
from __future__ import annotations

import json
from datetime import date

from ..db import DB
from ..settings import SECTOR_LABELS
from ..util import log, truncate
from . import notion as N
from .blocks import to_blocks


def publish_briefing(notion: N.Notion, day: date, title: str, markdown: str, *, status: str,
                     themes: list[str], sectors_moved: list[str], quiet: bool, deep_dive: str = "") -> str:
    props = {
        "Briefing": N.title(title), "Date": N.date(day.isoformat()), "Status": N.select(status),
        "Top themes": N.multi([truncate(t, 90).replace(",", "") for t in themes[:5]]),
        "Sectors that moved": N.multi(sectors_moved), "Quiet day": N.checkbox(quiet),
        "Deep dive": N.rich(deep_dive),
    }
    blocks = to_blocks(markdown)
    existing = notion.query("briefings", filter={"property": "Date", "date": {"equals": day.isoformat()}})
    if existing:
        page_id = existing[0]["id"]
        notion.update_page(page_id, props)
        notion.archive_children(page_id)
        notion.append(page_id, blocks)
        return existing[0].get("url", page_id)
    page = notion.create_page("briefings", props, blocks, icon="📈")
    return page.get("url", page["id"])


def push_views(notion: N.Notion, db: DB) -> int:
    rows = db.q("SELECT v.*, c.claim, c.via, c.sector, i.source_name FROM views v JOIN claims c ON c.id=v.claim_id "
                "JOIN items i ON i.id=c.item_id WHERE v.pushed=0 LIMIT 300")
    n, fails = 0, 0
    for r in rows:
        if fails >= 3:
            log.warning("Stopped pushing views after 3 failures; they'll be retried next run")
            break
        try:
            notion.create_page("views", {
                "View": N.title(truncate(r["claim"], 200)), "Person": N.rich(r["speaker"]),
                "Outlet": N.rich(r["via"] or r["source_name"] or ""), "Debate": N.select(r["debate"]),
                "Sector": N.select(SECTOR_LABELS.get(r["sector"], "Elsewhere")),
                "Stance": N.select((r["stance"] or "neutral").capitalize()),
                "Previous stance": N.select(r["prev_stance"].capitalize() if r["prev_stance"] else None),
                "Changed view": N.checkbox(r["changed"]), "Priority voice": N.checkbox(r["priority"]),
                "Date": N.date(r["date"]), "Link": N.url(r["url"] if str(r["url"]).startswith("http") else None),
            })
            db.ex("UPDATE views SET pushed=1 WHERE id=?", (r["id"],))
            n += 1
            fails = 0
        except Exception as e:
            fails += 1
            log.warning("Could not push view %s: %s", r["id"], e)
    return n


def push_ideas(notion: N.Notion, db: DB) -> None:
    for r in db.q("SELECT * FROM ideas WHERE notion_page_id IS NULL"):
        page = notion.create_page("ideas", {
            "Idea": N.title(f"Long {r['long']} / short {r['short']}"), "Long": N.rich(r["long"]),
            "Short": N.rich(r["short"]), "Sector": N.select(SECTOR_LABELS.get(r["sector"] or "", "Elsewhere")),
            "Origin": N.select(r["origin"]), "Against consensus": N.checkbox(r["against_consensus"]),
            "Confidence": N.select(r["confidence"]), "Status": N.select(r["status"]),
            "Entry date": N.date(r["created"]), "Hedge ratio": N.number(round(r["hedge_ratio"], 3)),
            "Correlation 1y": N.number(round(r["corr"], 3)), "Stop (spread)": N.number(round(r["stop_spread"], 4)),
            "Paper P&L": N.number(round(r["pnl"] or 0, 4)), "Thesis": N.rich(truncate(r["thesis"] or "", 1900)),
            "What proves it wrong": N.rich(truncate(r["wrong"] or "", 1900)),
        })
        db.ex("UPDATE ideas SET notion_page_id=? WHERE id=?", (page["id"], r["id"]))
    for r in db.q("SELECT * FROM ideas WHERE notion_page_id IS NOT NULL AND (status='Open' OR closed>=date('now','-1 day'))"):
        try:
            notion.update_page(r["notion_page_id"], {"Paper P&L": N.number(round(r["pnl"] or 0, 4)),
                                                     "Status": N.select(r["status"])})
        except Exception as e:
            log.warning("Could not update idea %s: %s", r["id"], e)


def push_discovery(notion: N.Notion, db: DB) -> None:
    for r in db.q("SELECT * FROM discovery WHERE pushed=0"):
        notion.create_page("discovery", {
            "Publication": N.title(r["name"]), "Link": N.url(r["pub_url"]), "Example post": N.url(r["example_post"]),
            "Score": N.number(r["score"]), "Why": N.rich(r["why"] or ""),
            "Sectors": N.multi(json.loads(r["sectors"] or "[]")), "Found via": N.select(r["found_via"]),
            "Decision": N.select("Undecided"), "Week": N.date(r["first_seen"]),
        })
        db.ex("UPDATE discovery SET pushed=1 WHERE pub_url=?", (r["pub_url"],))


def sync_discovery_decisions(notion: N.Notion, db: DB) -> list[str]:
    """Tracked publications become paused Sources rows; ignored ones are remembered."""
    added = []
    for row in notion.query("discovery"):
        decision = N.prop(row, "Decision")
        link = N.prop(row, "Link")
        if not link:
            continue
        cur = db.one("SELECT decision FROM discovery WHERE pub_url=?", (link,))
        if cur and cur["decision"] == decision:
            continue
        db.ex("UPDATE discovery SET decision=? WHERE pub_url=?", (decision, link))
        if decision == "Track":
            notion.create_page("sources", {
                "Source": N.title(N.prop(row, "Publication") or link), "Type": N.select("Substack"),
                "Feed URL": N.url(link.rstrip("/") + "/feed"), "Active": N.checkbox(True),
                "Notes": N.rich("Added from Discovery"),
                "Sectors": N.multi(N.prop(row, "Sectors") or []),
            })
            added.append(link)
    return added


def propose_debates(notion: N.Notion, keys: list[str], existing: set[str]) -> None:
    if not keys:
        return
    # Include inactive (already proposed or rejected) debates so nothing is proposed twice.
    existing = set(existing) | {N.prop(r, "Key") for r in notion.query("debates") if N.prop(r, "Key")}
    for k in keys:
        if k in existing:
            continue
        notion.create_page("debates", {"Question": N.title(f"(proposed) {k}"), "Key": N.rich(k),
                                       "Active": N.checkbox(False)})


TYPE_LABEL = {"substack": "Substack", "rss": "RSS", "email": "Email", "web_page": "Web page",
              "podcast": "Podcast", "bluesky": "Bluesky", "press_mentions": "Press mentions", "api": "API"}


def sync_sources(notion: N.Notion, yaml_sources: list) -> dict:
    """One-off: fill empty Feed URLs in Notion from the YAML and add YAML-only sources (paused)."""
    rows = {(N.prop(r, "Source") or "").lower(): r for r in notion.query("sources")}
    filled, added = [], []
    for s in yaml_sources:
        row = rows.get(s.name.lower())
        if row is None:
            notion.create_page("sources", {
                "Source": N.title(s.name), "Type": N.select(TYPE_LABEL.get(s.type, "RSS")),
                "Feed URL": N.url(s.feed_url), "Active": N.checkbox(bool(s.active and s.feed_url)),
                "Sectors": N.multi([SECTOR_LABELS.get(k, k) for k in s.sectors if k in SECTOR_LABELS]),
                "Notes": N.rich((s.notes or "Added from config/briefing.yaml")[:1900]),
            })
            added.append(s.name)
        elif not N.prop(row, "Feed URL") and s.feed_url:
            notion.update_page(row["id"], {"Feed URL": N.url(s.feed_url),
                                           "Type": N.select(TYPE_LABEL.get(s.type, "RSS"))})
            filled.append(s.name)
        elif not N.prop(row, "Feed URL") and s.type == "press_mentions":
            notion.update_page(row["id"], {"Type": N.select("Press mentions")})
            filled.append(s.name)
    return {"filled": filled, "added": added}
