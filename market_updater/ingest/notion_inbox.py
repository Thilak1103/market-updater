"""The Notion Inbox: links Thilak saves by hand (X posts, articles, audio).

Tag "Include" -> read the link and treat it like any other source. If the site blocks
automated reading (Citadel, Substack), paste the article text into the Inbox row's page
body and that text is used instead.
Tag "Add source" -> find the feed and add a paused row to the Sources database.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..publish import notion as N
from ..util import get_with_retry, html_to_text, log
from . import Item
from .rss import apple_episode, discover_feed
from .youtube import is_youtube, read_video


def process_inbox(notion: N.Notion) -> list[Item]:
    out: list[Item] = []
    rows = notion.query("inbox", filter={"property": "Processed", "checkbox": {"equals": False}})
    for row in rows:
        link = N.prop(row, "Link")
        tag = N.prop(row, "Tag") or "Include"
        title = N.prop(row, "Title") or link or "Saved link"
        note = N.prop(row, "Note") or ""
        try:
            if not link and (tag == "Add source" or len(notion.page_text(row["id"])) < 200):
                continue
            if tag == "Add source":
                if is_youtube(link):
                    from .youtube import channel_feed
                    feed, kind = channel_feed(link), "YouTube"
                else:
                    feed = discover_feed(link)
                    kind = "RSS" if feed else "Web page"
                notion.create_page("sources", {
                    "Source": N.title(title), "Type": N.select(kind),
                    "Feed URL": N.url(feed or link), "Active": N.checkbox(False),
                    "Needs checking": N.checkbox(True),
                    "Notes": N.rich(f"Added from Inbox. {note}".strip()),
                })
            elif is_youtube(link) and tag != "Add source":
                pasted = notion.page_text(row["id"])
                vid = read_video(link) if len(pasted) < 200 else {**read_video(link, allow_audio=False), "text": pasted}
                if not vid["text"]:
                    log.warning("YouTube video %s could not be read (no captions, audio blocked). Open the "
                                "video, click '...more' then 'Show transcript', and paste it into the Inbox "
                                "row's page to include it", link)
                    continue
                name = vid["channel"] or "YouTube"
                text = f"Thilak's note: {note}\n\n{vid['text']}" if note else vid["text"]
                out.append(Item(url=vid.get("url") or link, title=N.prop(row, "Title") or vid["title"] or title,
                                text=text, source_name=f"{name} (YouTube)", source_type="inbox",
                                kind="inbox", published=datetime.now(timezone.utc)))
            elif link and "podcasts.apple.com" in link and (ep := apple_episode(link)):
                desc = html_to_text(ep["description"]) if "<" in ep["description"] else ep["description"]
                out.append(Item(url=link, title=N.prop(row, "Title") or ep["title"] or title,
                                text=(f"Thilak's note: {note}\n\n" if note else "") + desc,
                                source_name=f"{ep['show']} (podcast)" if ep["show"] else "Inbox",
                                source_type="inbox", kind="podcast", audio_url=ep["audio_url"],
                                published=datetime.now(timezone.utc)))
            else:
                if link and link.split("?")[0].lower().endswith((".mp3", ".m4a", ".wav")):
                    out.append(Item(url=link, title=title, text=note, source_name="Inbox",
                                    source_type="inbox", kind="podcast", audio_url=link,
                                    published=datetime.now(timezone.utc)))
                else:
                    pasted = notion.page_text(row["id"])
                    if len(pasted) >= 200 or not link:
                        text = pasted
                    else:
                        r = get_with_retry(link)
                        text = html_to_text(r.text) if r.status_code == 200 else ""
                        if not text:
                            # Leave it unprocessed so pasting the text into the row later still works.
                            log.warning("Inbox link %s could not be read (HTTP %s); paste the text "
                                        "into the row's page to include it", link, r.status_code)
                            continue
                    if note:
                        text = f"Thilak's note: {note}\n\n{text}"
                    out.append(Item(url=link or f"notion-inbox:{row['id']}", title=title, text=text,
                                    source_name="Inbox", source_type="inbox", kind="inbox",
                                    published=datetime.now(timezone.utc)))
            notion.update_page(row["id"], {"Processed": N.checkbox(True)})
        except Exception as e:
            log.warning("Inbox item %s failed: %s", link, e)
    return out
