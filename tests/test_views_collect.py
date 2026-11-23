from datetime import date, datetime, timedelta, timezone

from market_updater.analysis.views import debate_states, record_views
from market_updater.ingest import Item
from market_updater.ingest.collect import lookback, store


def _claim(db, speaker, stance, day, debate="ai-capex-roi"):
    iid = db.add_item(url=f"https://x.com/{speaker}/{day}/{stance}", fingerprint=f"{speaker}{day}{stance}",
                      source_name="Test", source_type="rss", kind="article", title=f"{speaker} view",
                      published=f"{day}T08:00:00+00:00", text="t", is_subscriber=False, sectors=[],
                      repeat_of=None)
    return db.add_claim(iid, {"kind": "opinion", "speaker": speaker, "sector": "tmt", "debate": debate,
                              "stance": stance, "claim": "c", "evidence_quote": "t"})


def test_crowded_consensus_and_changed_view(settings, db):
    day = date(2026, 11, 18)
    for i in range(6):
        _claim(db, f"Analyst {i}", "bull", "2026-11-17")
    _claim(db, "Michael Burry", "bull", "2026-11-10")
    _claim(db, "Michael Burry", "bear", "2026-11-17")
    record_views(settings, db, datetime(2026, 11, 1, tzinfo=timezone.utc))
    st = next(s for s in debate_states(settings, db, day) if s.key == "ai-capex-roi")
    assert st.majority == "bull"
    assert st.crowded
    assert any(v["speaker"] == "Michael Burry" for v in st.changed)


def test_store_dedupes_url_and_fingerprint(db):
    a = Item("https://a.com/1", "Same story about Tesco results", "body", "A", "rss")
    b = Item("https://b.com/2", "Same story about Tesco results", "body", "B", "rss")
    assert store(db, [a, a, b]) == 1


def test_monday_lookback_covers_weekend():
    mon = datetime(2026, 11, 16, 5, tzinfo=timezone.utc)
    tue = mon + timedelta(days=1)
    assert mon - lookback(mon) == timedelta(hours=78)
    assert tue - lookback(tue) == timedelta(hours=30)


def test_earnings_review_waits_for_reaction_session(db):
    from market_updater.market.earnings import Report, add_reaction, reviewable
    from tests.conftest import synthetic_prices

    df = synthetic_prices(["AAA", "XLK"])            # last close 2026-11-19
    after_close = Report("AAA", "2026-11-19", hour="amc")
    add_reaction(after_close, df, "XLK", db)
    assert after_close.pending and not reviewable(after_close, df)

    before_open = Report("AAA", "2026-11-19", hour="bmo")
    add_reaction(before_open, df, "XLK", db)
    assert before_open.reaction_day == "2026-11-19" and reviewable(before_open, df)
    assert before_open.move_pct is not None and before_open.sector_move_pct is not None

    yesterday_amc = Report("AAA", "2026-11-18", hour="amc")   # reacted on the 19th
    add_reaction(yesterday_amc, df, None, db)
    assert reviewable(yesterday_amc, df)

    old = Report("AAA", "2026-11-17", hour="bmo")             # already reviewed last time
    add_reaction(old, df, None, db)
    assert not reviewable(old, df)


def test_published_today(db):
    from datetime import datetime, timezone
    from market_updater import run as R
    assert not R.published_today(db)
    rid = db.start_run("daily")
    db.finish_run(rid, "Failed")
    assert not R.published_today(db)
    rid = db.start_run("daily")
    db.finish_run(rid, "Published")
    assert R.published_today(db)
