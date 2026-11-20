"""End-to-end run with no network: fake model, synthetic prices, a stored item."""
from datetime import datetime, timezone

import market_updater.run as R
from market_updater.llm import FakeLLM
from market_updater.market import earnings as E
from market_updater.market import fundamentals as F
from market_updater.market import macro as M
from market_updater.market.macro import Obs
from tests.conftest import fake_responder


def _offline(monkeypatch, tmp_path):
    monkeypatch.setattr(R, "DATA_DIR", tmp_path)
    monkeypatch.setattr(M, "collect", lambda s: [Obs("DGS10", "UST 10y (%)", "2026-11-17", 4.12, 4.08, None, "FRED")])
    monkeypatch.setattr(E, "finnhub_calendar", lambda a, b: [
        {"symbol": "TSCO.L", "date": "2026-11-19", "epsActual": 0.12, "epsEstimate": 0.10}])
    monkeypatch.setattr(E, "add_transcripts", lambda rep, db, day: None)
    fund = {"name": "Co", "ev_ebitda": 9.5, "pe_ltm": 18.0, "ev_sales": 2.1, "sources": {"pe": "Yahoo (yfinance)"},
            "asof": "2026-11-18"}
    monkeypatch.setattr(F, "roic_fundamentals", lambda t, k=None: ({}, None))
    monkeypatch.setattr(F, "yahoo_fundamentals", lambda t: dict(fund, name=f"{t} plc"))
    import market_updater.publish.telegram as T
    monkeypatch.setattr(T, "send", lambda text: False)


def test_friday_dry_run_builds_every_section(settings, db, fake_prices, monkeypatch, tmp_path):
    _offline(monkeypatch, tmp_path)
    db.add_item(url="https://apolloacademy.com/ai", fingerprint="fp1", source_name="Apollo Daily Spark",
                source_type="rss", kind="article", title="AI capex is outrunning revenue",
                published="2026-11-19T22:00:00+00:00",
                text="Torsten Slok argues that AI capital spending is running far ahead of revenue growth this year.",
                is_subscriber=False, sectors=["tmt"], repeat_of=None)
    other = db.add_item(url="https://example.com/bull", fingerprint="fp2", source_name="Test", source_type="rss",
                        kind="article", title="Bull case", published="2026-11-19T20:00:00+00:00",
                        text="t", is_subscriber=False, sectors=["tmt"], repeat_of=None)
    db.ex("UPDATE items SET extracted=1 WHERE id=?", (other,))
    db.add_claim(other, {"kind": "opinion", "speaker": "Scott Rubner", "sector": "tmt", "debate": "ai-capex-roi",
                         "stance": "bull", "claim": "Flows into AI names remain strong.", "evidence_quote": "t"})
    for i in range(3):
        e = db.add_item(url=f"https://example.com/oil{i}", fingerprint=f"oil{i}", source_name="Test",
                        source_type="rss", kind="article", title=f"Oil story {i}",
                        published="2026-11-18T09:00:00+00:00", text="t", is_subscriber=False,
                        sectors=["energy"], repeat_of=None)
        db.ex("UPDATE items SET extracted=1 WHERE id=?", (e,))
        db.add_claim(e, {"kind": "fact", "speaker": "Reuters", "sector": "energy", "tickers": ["XLE"],
                         "claim": "Oil supply news.", "evidence_quote": "t"})
    db.ex("INSERT INTO elsewhere_log VALUES ('2026-11-19','energy','XLE +3%',3.0)")
    llm = FakeLLM(fake_responder)
    now = datetime(2026, 11, 20, 6, 15, tzinfo=timezone.utc)   # a Friday
    res = R.daily(dry_run=True, settings=settings, db=db, llm=llm, prices=fake_prices, notion=None,
                  now=now, collect=False)
    md = res.markdown
    for heading in ["## ☀️ Top line", "## 📊 The numbers that matter", "## 🎯 Trade ideas", "## 🏭 Your sectors",
                    "## 📅 Earnings reviews", "## 🌍 Elsewhere", "## 🏁 Trade idea scorecard"]:
        assert heading in md, heading
    for gone in ["Market overview", "## Sources", "Source health", "not investment advice", "Where views diverge"]:
        assert gone not in md, gone
    assert "Deep dive: Energy" in md and "### Why it is happening" in md and "claims provided" not in md and "- The next OPEC meeting" in md
    # Safety checks did their job
    assert "12.34%" not in md and "97.3%" not in md and "[c99999]" not in md and "claims supplied" not in md
    assert "[c1]" not in md and "[c" not in md and "](https://example.com/bull)" in md
    slok = db.one("SELECT c.speaker, i.url FROM claims c JOIN items i ON i.id=c.item_id WHERE c.speaker='Torsten Slok'")
    assert slok and slok["url"] == "https://apolloacademy.com/ai"
    # Three ideas: the bad pair and the reused leg are rejected; an invented peer is dropped from comps
    assert md.count("::: callout 📈 gray") == 3 and "NOTREAL" not in md
    assert "**Long Nvidia · Short AMD**" in md and "Why AMD (short):" in md and "Why Nvidia (long):" in md
    assert "Mkt cap" not in md and "ROIC" not in md and "EV/EBITDA" in md and "P/E fwd" in md
    assert "Hedge ratio" not in md and "Statistics" not in md and "The pair isolates" not in md
    assert "trading days (to " in md and "hedged" in md
    assert "Fee income" not in md          # the long case with an unsourced number was cut
    assert "TSCO.L" in md
    assert "Only " not in md                # three ideas passed, so no shortfall note
    gs = md[md.index("**Long Goldman Sachs"):]
    assert "TSCO.L" not in gs.split(":::")[2]   # a peer from another sector is kept out of the comps
    # Elsewhere is a table with a cause, and the sectors are one-paragraph callouts
    assert "| Sector | Move | What's behind it |" in md and "Oil supply news moved the group" in md
    assert "::: callout 💻 blue" in md and "**TMT** — Voices are turning bearish" in md
    # The fake quote that isn't in the text was dropped
    assert db.one("SELECT COUNT(*) n FROM claims")["n"] == 5
    assert (tmp_path / "out" / "2026-11-20.md").exists()
    assert res.status in ("Published", "Partial")


def test_model_outage_still_publishes_market_data(settings, db, fake_prices, monkeypatch, tmp_path):
    _offline(monkeypatch, tmp_path)
    from market_updater.llm import LLMUnavailable

    def down(purpose, system, user):
        raise LLMUnavailable("both providers down")

    res = R.daily(dry_run=True, settings=settings, db=db, llm=FakeLLM(down), prices=fake_prices,
                  notion=None, now=datetime(2026, 11, 18, 6, 15, tzinfo=timezone.utc), collect=False)
    assert "## 📊 The numbers that matter" in res.markdown and "S&P 500" in res.markdown
    assert res.status == "Partial"
