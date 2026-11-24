from market_updater.publish.blocks import rich_text, to_blocks
from market_updater.publish.notion_settings import parse_tickers
from market_updater.settings import parse_setting


def test_blocks_cover_every_markdown_feature():
    md = ("## Title\n### Sub\nPara with **bold**, *it* and [link](https://a.com).\n- bullet\n1. num\n"
          "> quote\n---\n| H1 | H2 |\n|---|---|\n| a | b |\n::: toggle More\n- inside\n| x | y |\n:::\n")
    kinds = [b["type"] for b in to_blocks(md)]
    assert kinds == ["heading_2", "heading_3", "paragraph", "bulleted_list_item", "numbered_list_item",
                     "quote", "divider", "table", "toggle"]
    toggle = to_blocks(md)[-1]["toggle"]
    assert [c["type"] for c in toggle["children"]] == ["bulleted_list_item", "table"]


def test_rich_text_splits_long_text_and_links():
    rt = rich_text("x" * 4000 + " [l](https://b.com)")
    assert all(len(r["text"]["content"]) <= 1900 for r in rt)
    assert rt[-1]["text"]["link"]["url"] == "https://b.com"


def test_parse_tickers_and_settings():
    assert parse_tickers("NVDA (Nvidia), TSCO.L Tesco, to find") == ["NVDA", "TSCO.L"]
    assert parse_setting("int", "4") == 4
    assert parse_setting("range", "3 to 4") == [3, 4]


def test_seed_settings_are_coherent(settings):
    assert len(settings.core_sectors) == 4
    assert "NVDA" in settings.all_watchlist
    assert settings.sector_of_ticker("TSCO.L") == "staples_retail"
    for s in settings.sources:
        assert s.type, s.name


def test_notion_rows_without_urls_fall_back_to_yaml(settings):
    from market_updater.settings import Source, merge_sources
    rows = [Source("Marc Rubinstein", "rss"), Source("Richard Hyman", "rss"), Source("New one", "rss")]
    merged = {s.name: s for s in merge_sources(rows, settings.sources)}
    assert merged["Marc Rubinstein"].feed_url == "https://www.netinterest.co/feed"
    assert merged["Richard Hyman"].type == "press_mentions"
    assert merged["New one"].feed_url is None and len(merged) == 3


def test_clean_env_strips_pasted_line_breaks(monkeypatch):
    import os
    from market_updater.util import clean_env
    monkeypatch.setenv("NOTION_TOKEN", "ntn_abc123\n")
    monkeypatch.setenv("GROQ_API_KEY", "  gsk_x ")
    monkeypatch.setenv("FRED_API_KEY", "fine")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "\n")
    assert sorted(clean_env()) == ["GROQ_API_KEY", "NOTION_TOKEN", "TELEGRAM_CHAT_ID"]
    assert os.environ["NOTION_TOKEN"] == "ntn_abc123" and os.environ["GROQ_API_KEY"] == "gsk_x"
    assert "TELEGRAM_CHAT_ID" not in os.environ


def test_callouts_colours_mentions_and_images():
    from market_updater.publish.blocks import depth, to_blocks
    md = ("::: callout 📈 gray\n**Long A · Short B** — headline\n**Why:** it <span color=\"red\">fell 2%</span>.\n"
          "::: toggle Comps\n| a | b |\n|---|---|\n| 1 | 2 |\n:::\n![chart](upload:abc123)\n"
          "See the [dashboard](notion:41e25b170278816ca585e60fd34308b4).\n:::")
    [c] = to_blocks(md)
    assert c["type"] == "callout" and c["callout"]["color"] == "gray_background"
    assert c["callout"]["icon"]["emoji"] == "📈"
    assert c["callout"]["rich_text"][0]["annotations"]["bold"] is True
    kids = c["callout"]["children"]
    assert [k["type"] for k in kids] == ["paragraph", "toggle", "image", "paragraph"]
    red = [r for r in kids[0]["paragraph"]["rich_text"] if r.get("annotations", {}).get("color") == "red"]
    assert red and red[0]["text"]["content"] == "fell 2%"
    assert kids[2]["image"]["file_upload"]["id"] == "abc123"
    assert any(r["type"] == "mention" for r in kids[3]["paragraph"]["rich_text"])
    assert depth(c) == 4      # callout > toggle > table > rows: too deep for one request


def test_append_splits_deep_trees():
    from market_updater.publish.blocks import to_blocks
    from market_updater.publish.notion import Notion
    calls = []

    class Fake(Notion):
        def __init__(self):
            self.token, self.ids = "x", {}

        def _req(self, method, path, json=None):
            calls.append((path, json))
            return {"results": [{"id": f"b{len(calls)}-{i}"} for i, _ in enumerate((json or {}).get("children", []))]}

    md = "::: callout 📈 gray\nHead\n::: toggle Comps\n| a | b |\n|---|---|\n| 1 | 2 |\n:::\n:::\nAfter"
    Fake().append("page", to_blocks(md))
    first = calls[0][1]["children"]
    assert "children" not in first[0]["callout"] and first[1]["type"] == "paragraph"
    assert calls[1][0] == "/blocks/b1-0/children"        # the callout's toggle goes in second
    assert calls[1][1]["children"][0]["toggle"]["children"][0]["type"] == "table"


def test_stocks_i_follow_setting():
    from market_updater.settings import parse_setting
    assert parse_setting("tickers", "ANT, nvda; TSLA") == ["ANT", "NVDA", "TSLA"]
