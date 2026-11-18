"""The Notion Inbox: links Thilak saves by hand (X posts, articles, audio).

Tag "Include" -> read the link and treat it like any other source.
Tag "Add source" -> find the feed and add a paused row to the Sources database.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..publish import notion as N
from ..util import get_with_retry, html_to_text, log
from . import Item
from .rss import discover_feed


def process_inbox(notion: N.Notion) -> list[Item]:
    out: list[Item] = []
    rows = notion.query("inbox", filter={"property": "Processed", "checkbox": {"equals": False}})
    for row in rows:
        link = N.prop(row, "Link")
        tag = N.prop(row, "Tag") or "Include"
        title = N.prop(row, "Title") or link or "Saved link"
        note = N.prop(row, "Note") or ""
        try:
            if not link:
                continue
            if tag == "Add source":
                feed = discover_feed(link)
                notion.create_page("sources", {
                    "Source": N.title(title), "Type": N.select("RSS" if feed else "Web page"),
                    "Feed URL": N.url(feed or link), "Active": N.checkbox(False),
                    "Needs checking": N.checkbox(True),
                    "Notes": N.rich(f"Added from Inbox. {note}".strip()),
                })
            else:
                if link.split("?")[0].lower().endswith((".mp3", ".m4a", ".wav")):
                    out.append(Item(url=link, title=title, text=note, source_name="Inbox",
                                    source_type="inbox", kind="podcast", audio_url=link,
                                    published=datetime.now(timezone.utc)))
                else:
                    r = get_with_retry(link)
                    text = html_to_text(r.text) if r.status_code == 200 else ""
                    if note:
                        text = f"Thilak's note: {note}\n\n{text}"
                    out.append(Item(url=link, title=title, text=text or note or title,
                                    source_name="Inbox", source_type="inbox", kind="inbox",
                                    published=datetime.now(timezone.utc)))
            notion.update_page(row["id"], {"Processed": N.checkbox(True)})
        except Exception as e:
            log.warning("Inbox item %s failed: %s", link, e)
    return out
