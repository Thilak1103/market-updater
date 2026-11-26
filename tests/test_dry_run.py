import pytest
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
    import market_updater.market.positioning as POS
    monkeypatch.setattr(POS, "fetch", lambda t: {
        "ticker": t, "price": 100.0, "short_pct_float": 0.032, "days_to_cover": 1.5, "target_mean": 120.0,
        "ratings": {"strongBuy": 5, "buy": 10, "hold": 6, "sell": 1, "strongSell": 0},
        "eps_up_30d": 4, "eps_down_30d": 1})
    import market_updater.compose.pitch as PT
    monkeypatch.setattr(PT, "yahoo_info", lambda t: {"currency": "GBp", "marketCap": 48_400_000_000,
                                                     "freeCashflow": 2_800_000_000, "forwardPE": 17.9})


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
    assert "| Worth watching |" in res.markdown
    for heading in ["## ☀️ Top line", "## 📊 The numbers that matter", "## 🎯 Today's pitch", "## 🏭 Your sectors",
                    "## 📅 Earnings reviews", "## 🌍 Elsewhere", "## 🏁 Pitch scorecard"]:
        assert heading in md, heading
    for gone in ["Market overview", "## Sources", "Source health", "not investment advice", "Where views diverge"]:
        assert gone not in md, gone
    assert "Deep dive: Energy" in md and "### Why it is happening" in md and "claims provided" not in md and "- The next OPEC meeting" in md
    # Safety checks did their job
    assert "12.34%" not in md and "97.3%" not in md and "[c99999]" not in md and "claims supplied" not in md
    assert "[c1]" not in md and "[c" not in md and "](https://example.com/bull)" in md
    slok = db.one("SELECT c.speaker, i.url FROM claims c JOIN items i ON i.id=c.item_id WHERE c.speaker='Torsten Slok'")
    assert slok and slok["url"] == "https://apolloacademy.com/ai"
    # The daily pitch: one stock, short version with a link to the full pitch
    assert md.count("::: callout 📈 green") == 1 and "Ghost plc" not in md
    assert "**LONG London Stock Exchange Group (LSEG.L)**" in md and "· 6–12 months · entry" in md
    assert "**Base**" in md and "Bull" in md and "Bear" in md and "Reviewer 8/10" in md
    assert "**Why:** 1. The AI fear is aimed at a fifth of the business." in md
    assert "**Next test:** 26 Feb 2027, FY results: 2027 guidance above consensus" in md
    assert "**Biggest risk:** Buybacks fall." in md and "Full pitch in the pitch dashboard" in md
    assert "12.34%" not in md
    assert "TSCO.L" in md
    assert "## 📌 Stocks you follow" in md and "(ANT)" in md
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


def test_a_pitch_every_day_even_when_nothing_clears_review(settings, db, fake_prices, monkeypatch, tmp_path):
    _offline(monkeypatch, tmp_path)
    settings.raw["pitch"]["min_review_score"] = 10      # nothing can pass
    llm = FakeLLM(fake_responder)
    now = datetime(2026, 11, 20, 6, 15, tzinfo=timezone.utc)
    md = R.daily(dry_run=True, settings=settings, db=db, llm=llm, prices=fake_prices, notion=None,
                 now=now, collect=False).markdown
    assert md.count("::: callout 📈 green") == 1
    assert "**Where the reviewer pushed back:**" in md
    assert "You aren't following any pitches yet" in md


def test_reverse_dcf_and_tracking(db):
    from datetime import date
    import pandas as pd
    from market_updater.compose import pitch as PT
    md, out = PT.reverse_dcf(0.058, 9888.0, "p")
    row = next(r for r in out["rows"] if r["g"] == 0.08 and r["k"] == 0.085)
    assert row["years"] in (1, 2) and 11500 < row["value5"] < 13500 and "9,888" not in md
    assert PT.fcf_yield({"freeCashflow": 2.8e9, "marketCap": 48.4e9}) == pytest.approx(0.0579, abs=1e-3)
    assert PT.fcf_yield({"freeCashflow": 2.8e9, "marketCap": 48.4e11}) == pytest.approx(0.0579, abs=1e-3)
    db.ex("INSERT INTO pitches(created,ticker,name,direction,entry_price,base,bear,horizon_end,spec) VALUES "
          "('2026-11-20','LSEG.L','LSEG','Long',100,115,85,'2027-11-20','{}')")
    db.ex("INSERT INTO pitches(created,ticker,name,direction,entry_price,base,bear,horizon_end,spec) VALUES "
          "('2026-11-20','XYZ','XYZ','Short',100,80,120,'2027-11-20','{}')")

    class P:
        def fetch(self, tickers, period):
            idx = pd.bdate_range(end="2026-12-01", periods=3)
            return pd.DataFrame({"LSEG.L": [100, 105, 116], "XYZ": [100, 95, 90]}, index=idx)
    ups = {u["ticker"]: u for u in PT.update_open(db, P(), date(2026, 12, 1))}
    assert ups["LSEG.L"]["status"] == "Closed" and ups["LSEG.L"]["outcome"] == "Hit base case"
    assert ups["XYZ"]["status"] == "Open" and ups["XYZ"]["ret"] == pytest.approx(0.10)
    assert ups["XYZ"]["progress"] == pytest.approx(0.5)


def test_valuation_methods_survive_the_number_check():
    from market_updater.compose import pitch as PT
    p = PT.Pitch(ticker="STLAM.MI", name="Stellantis", direction="Short", sector="Autos", market="Europe",
                 currency="EUR", unit="EUR", entry=3.4655, horizon_end="2027-11-26")
    spec = {"base": {"price": 2.17, "how": "Our 2027 EPS of €0.57 at the current 3.8x multiple."},
            "bull": {"price": 1.75, "how": "No growth on 2026 EPS."},
            "bear": {"price": 4.96, "how": "The Street's average target of €4.96."}}
    out = PT._clean_spec(spec, {}, [], set(), p)
    assert out["base"]["how"].endswith("(our calculation).") and "0.57" in out["base"]["how"]
    assert "4.96" in out["bear"]["how"]
    assert PT._pct(2.17, p) == "+37%" and PT._pct(4.96, p) == "−43%"
