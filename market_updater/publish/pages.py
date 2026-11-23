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
        return existing[0].get("url", page_id), page_id
    page = notion.create_page("briefings", props, blocks, icon="📰")
    return page.get("url", page["id"]), page["id"]


def publish_reading(notion: N.Notion, day: date, out, markdown: str, claims_by_id: dict) -> str:
    """Create (or refresh) today's reading page for one core sector. Returns its page id."""
    from ..analysis.checks import link_citations
    from ..compose.sections import ICONS
    label = SECTOR_LABELS.get(out.sector.key, out.sector.name)
    props = {"Reading": N.title(f"{out.sector.name} reading, {day.strftime('%a %d %b')}"),
             "Date": N.date(day.isoformat()), "Sector": N.select(label), "Pieces": N.number(len(out.reading))}
    blocks = to_blocks(link_citations(markdown, claims_by_id))
    existing = notion.query("reading", filter={"and": [
        {"property": "Date", "date": {"equals": day.isoformat()}},
        {"property": "Sector", "select": {"equals": label}}]})
    if existing:
        page_id = existing[0]["id"]
        notion.update_page(page_id, props)
        notion.archive_children(page_id)
        notion.append(page_id, blocks)
        return page_id.replace("-", "")
    page = notion.create_page("reading", props, blocks, icon=ICONS.get(out.sector.key, "📚"))
    return page["id"].replace("-", "")


def link_reading(notion: N.Notion, reading_id: str, briefing_url: str | None) -> None:
    notion.update_page(reading_id, {"Briefing": N.url(briefing_url)})


def update_home(notion: N.Notion, settings, day: date, briefing_id: str, headline: str) -> None:
    """Point the 'Today's briefing' box at the top of the Market Updater page at today's page."""
    home = (settings.get("notion.pages", {}) or {}).get("home")
    if not home:
        return
    box = next((b for b in notion.children(home) if b.get("type") == "callout"
                and (b["callout"].get("icon") or {}).get("emoji") == "📰"), None)
    rich = [{"type": "text", "text": {"content": f"Today's briefing ({day.strftime('%a %d %b')}): "},
             "annotations": {"bold": True}}]
    if headline:
        rich.append({"type": "text", "text": {"content": headline.rstrip() + " "}})
    rich.append({"type": "mention", "mention": {"type": "page", "page": {"id": briefing_id}}})
    if box:
        notion.update_block(box["id"], {"callout": {"rich_text": rich}})
    else:
        log.warning("No 📰 box on the Market Updater page; add a callout with that icon to show today's briefing")


def sync_following(notion: N.Notion, db: DB) -> int:
    """Copy the Following ticks from the Ideas database into SQLite. Returns how many are followed."""
    ticked = {p["id"] for p in notion.query("ideas", filter={"property": "Following", "checkbox": {"equals": True}})}
    built = {p["id"] for p in notion.query("ideas", filter={"property": "Build out", "checkbox": {"equals": True}})}
    for r in db.q("SELECT id, notion_page_id FROM ideas WHERE notion_page_id IS NOT NULL"):
        db.ex("UPDATE ideas SET following=?, build_out=? WHERE id=?",
              (int(r["notion_page_id"] in ticked), int(r["notion_page_id"] in built), r["id"]))
    return len(ticked)


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


CITE_RE = __import__("re").compile(r"\s?\[c\d+\]")


def _idea_props(r) -> dict:
    return {k: v for k, v in {
        "Return so far": N.number(round(r["pnl"] or 0, 4)), "Status": N.select(r["status"]),
        "Outcome": N.select(r["outcome"] or "Open"),
        "Best so far": N.number(round(r["best"] or 0, 4)), "Worst so far": N.number(round(r["worst"] or 0, 4)),
        "Progress to target": N.number(round((r["pnl"] or 0) / r["target_return"], 3)) if r["target_return"] else None,
        "Thesis check": N.select(r["thesis_status"]) if r["thesis_status"] else None,
        "Thesis note": N.rich(truncate(CITE_RE.sub("", r["thesis_note"] or ""), 1900)) if r["thesis_note"] else None,
    }.items() if v is not None}


def push_ideas(notion: N.Notion, db: DB, day_counts: dict[int, str] | None = None,
               charts: dict[tuple[str, str], bytes] | None = None) -> None:
    """Create a page per new idea (the pitch is the page body) and refresh open ideas' tracking."""
    from ..analysis.checks import CITE, link_citations
    for r in db.q("SELECT * FROM ideas WHERE notion_page_id IS NULL"):
        spec = json.loads(r["spec"] or "{}")
        ids = [int(x) for x in json.loads(r["source_names"] or "[]")]
        claims = {}
        if ids:
            q = ("SELECT c.id, i.url FROM claims c JOIN items i ON i.id=c.item_id WHERE c.id IN (%s)"
                 % ",".join("?" * len(ids)))
            claims = {c["id"]: {"url": c["url"]} for c in db.q(q, ids)}
        png = (charts or {}).get((r["long"], r["short"]))
        up = notion.upload_file(png, f"spread-{r['long']}-{r['short']}.png") if png else None
        body = spec.get("markdown", "").replace(
            "{{chart}}", f"![The hedged spread over the last year, with entry, target and stop](upload:{up})" if up else "")
        body = "\n".join(l for l in link_citations(body, claims).splitlines() if "Tick **Following**" not in l)
        thesis = CITE.sub("", r["thesis"] or "").replace("  ", " ")
        props = {
            "Idea": N.title(f"Long {r['long']} / short {r['short']}" + (f": {r['headline']}" if r["headline"] else "")),
            "Long": N.rich(r["long"]), "Short": N.rich(r["short"]),
            "Sector": N.select(SECTOR_LABELS.get(r["sector"] or "", "Elsewhere")),
            "Origin": N.select(r["origin"]), "Against consensus": N.checkbox(r["against_consensus"]),
            "Conviction": N.select(r["confidence"]), "Entry date": N.date(r["created"]),
            "Hedge ratio": N.number(round(r["hedge_ratio"], 3)), "Correlation 1y": N.number(round(r["corr"], 3)),
            "Stop (spread)": N.number(round(r["stop_spread"], 4)),
            "Target return": N.number(round(r["target_return"], 4)) if r["target_return"] is not None else N.number(None),
            "Stop return": N.number(round(r["stop_return"], 4)) if r["stop_return"] is not None else N.number(None),
            "Horizon end": N.date(r["horizon_end"]), "Day": N.rich("1 of 20"),
            "Briefing": N.url(r["briefing_url"]),
            "Thesis": N.rich(truncate(thesis, 1900)),
            "What proves it wrong": N.rich(truncate(CITE.sub("", r["wrong"] or ""), 1900)),
            **_idea_props(r),
        }
        page = notion.create_page("ideas", props, to_blocks(body) if body else None, icon="📈")
        db.ex("UPDATE ideas SET notion_page_id=? WHERE id=?", (page["id"], r["id"]))
    for r in db.q("SELECT * FROM ideas WHERE notion_page_id IS NOT NULL AND (status='Open' OR closed>=date('now','-1 day'))"):
        props = _idea_props(r)
        if day_counts and r["id"] in day_counts:
            props["Day"] = N.rich(day_counts[r["id"]])
        try:
            notion.update_page(r["notion_page_id"], props)
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
