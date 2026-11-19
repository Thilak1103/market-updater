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
