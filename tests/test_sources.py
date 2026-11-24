

def test_forwarded_newsletter_credits_the_original_sender():
    from market_updater.ingest.email_inbox import forwarded_origin
    text = ("________________________________\nFrom: Unhedged <unhedged@newsletters.ft.com>\nSent: 24 November 2026\n"
            "To: Saravanan,T\nSubject: The Jensen put\n\nBody of the letter...")
    assert forwarded_origin("Saravanan,T", "FW: The Jensen put", text) == ("Unhedged", "The Jensen put")
    assert forwarded_origin("Doomberg", "Energy notes", text) == ("Doomberg", "Energy notes")


def test_youtube_links_are_recognised():
    from market_updater.ingest.youtube import video_id
    for url in ("https://youtu.be/IgvDUUn3uuE?si=NeJKp8JeZXnkYFm9",
                "https://www.youtube.com/watch?v=IgvDUUn3uuE&t=30s",
                "https://m.youtube.com/shorts/IgvDUUn3uuE",
                "https://www.youtube.com/live/IgvDUUn3uuE"):
        assert video_id(url) == "IgvDUUn3uuE", url
    assert video_id("https://www.youtube.com/@OddLots") is None
    assert video_id("https://example.com/watch?v=IgvDUUn3uuE") is None


def test_youtube_inbox_link_becomes_an_item(monkeypatch):
    from market_updater.ingest import notion_inbox as NI

    class FakeNotion:
        updated = []
        def query(self, *a, **k):
            return [{"id": "row1"}]
        def page_text(self, _):
            return ""
        def update_page(self, rid, props):
            self.updated.append(rid)

    props = {"Link": "https://youtu.be/IgvDUUn3uuE", "Tag": "Include", "Title": None, "Note": "Watch the bit on credit"}
    monkeypatch.setattr(NI.N, "prop", lambda row, name: props.get(name))
    monkeypatch.setattr(NI, "read_video", lambda url, allow_audio=True: {
        "title": "Private credit cracks", "channel": "Bloomberg Podcasts", "text": "Words " * 400,
        "how": "captions", "seconds": 0.0, "url": "https://www.youtube.com/watch?v=IgvDUUn3uuE"})
    fake = FakeNotion()
    items = NI.process_inbox(fake)
    assert len(items) == 1 and fake.updated == ["row1"]
    it = items[0]
    assert it.title == "Private credit cracks" and it.source_name == "Bloomberg Podcasts (YouTube)"
    assert it.text.startswith("Thilak's note: Watch the bit on credit")

    # Unreadable video: the row stays unprocessed so a pasted transcript still works later.
    monkeypatch.setattr(NI, "read_video", lambda url, allow_audio=True: {
        "title": "", "channel": "", "text": "", "how": "failed", "seconds": 0.0})
    fake2 = FakeNotion(); fake2.updated = []
    assert NI.process_inbox(fake2) == [] and fake2.updated == []


def test_saved_section_lists_inbox_items_with_their_summary():
    import json
    from market_updater.compose import saved as SV
    md = SV.markdown([{"url": "https://www.youtube.com/watch?v=IgvDUUn3uuE", "title": "Private credit cracks",
                       "source_name": "Bloomberg Podcasts (YouTube)", "text": "...",
                       "summary": json.dumps({"who": "Joe Weisenthal (host)", "points": ["Defaults rising"],
                                              "worth_it": "Yes, minutes 10 to 25."})}])
    assert "**[Private credit cracks](https://www.youtube.com/watch?v=IgvDUUn3uuE)**" in md
    assert "- Defaults rising" in md and "**Worth your time?** Yes" in md
