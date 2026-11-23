from market_updater.market import comps as C
from market_updater.market.pairs import pair_stats
from tests.conftest import synthetic_prices


def test_pair_stats_on_correlated_names():
    df = synthetic_prices(["AAA", "BBB"])
    st = pair_stats(df, "AAA", "BBB")
    assert st is not None
    assert 0.3 < st.beta < 3
    assert st.corr > 0.5
    assert st.stop_spread < st.spread_now


def test_pair_stats_missing_ticker():
    assert pair_stats(synthetic_prices(["AAA"]), "AAA", "ZZZ") is None


def test_comps_table_median_and_premium():
    data = {"A": {"name": "Alpha", "ev_ebitda": 10.0, "pe_ltm": 20.0, "sources": {"ev_ebitda": "ROIC.ai"}},
            "B": {"name": "Beta", "ev_ebitda": 8.0, "pe_ltm": 16.0},
            "P1": {"ev_ebitda": 9.0, "pe_ltm": 18.0}, "P2": {"ev_ebitda": 11.0, "pe_ltm": 22.0}}
    out = C.build(["A", "B"], ["P1", "P2"], lambda t: data.get(t, {}), lambda t, k: 1.0)
    md = out["markdown"]
    assert "**Alpha (A)**" in md and "| P1 |" in md and "| Peer median |" in md
    assert out["median"]["ev_ebitda"] == 10.0
    assert round(out["premium"]["B"]["ev_ebitda"]) == -20
    assert "ROIC.ai" in md


def test_targets_tracking_and_scorecard(db, fake_prices, settings):
    import math
    from datetime import date
    from market_updater.compose import ideas as I
    from market_updater.market.pairs import pair_stats
    fake_prices.load(["NVDA", "AMD"])
    st = pair_stats(fake_prices.frame, "NVDA", "AMD")
    target, tret, sret, end, label = I.plan(st, {}, date(2026, 11, 20))
    assert target > st.spread_now and tret > 0 > sret and end == "2026-12-18"
    db.ex("INSERT INTO ideas(created,long,short,hedge_ratio,corr,entry_long,entry_short,entry_spread,spread_std,"
          "stop_spread,target_spread,target_return,following) VALUES ('2026-11-19','NVDA','AMD',?,?,?,?,?,?,?,?,?,1)",
          (st.beta, st.corr, st.entry_long * 0.8, st.entry_short, math.log(st.entry_long * 0.8) - st.beta * math.log(st.entry_short),
           st.spread_std_60, -99, math.log(st.entry_long * 0.85) - st.beta * math.log(st.entry_short), 0.05))
    [u] = I.update_open(db, fake_prices, settings, date(2026, 11, 20))
    assert u["outcome"] == "Hit target" and u["following"] and u["pnl"] > 0.2
    card = I.scorecard(db)
    assert "| Hit target | 1 | 1 |" in card
    box = I.following_markdown([dict(u, status="Open", progress=0.5)], "abc")
    assert "callout ⭐ yellow" in box and "50% of the way to target" in box and "notion:abc" in box


def test_no_repeats_and_book_table(db):
    from datetime import date
    from market_updater.compose import ideas as I
    db.ex("INSERT INTO ideas(created,long,short,hedge_ratio,corr,entry_long,entry_short,entry_spread,spread_std,"
          "stop_spread,status) VALUES ('2026-11-20','NVDA','AMD',1,0.8,1,1,0,0.1,-1,'Closed')")
    db.ex("INSERT INTO ideas(created,long,short,hedge_ratio,corr,entry_long,entry_short,entry_spread,spread_std,"
          "stop_spread,status) VALUES ('2026-10-01','JPM','BAC',1,0.8,1,1,0,0.1,-1,'Open')")
    blocked = I.recently_pitched(db, date(2026, 11, 23), 21)
    assert {"NVDA", "AMD", "JPM", "BAC"} <= blocked
    upd = [{"id": 2, "long": "JPM", "short": "BAC", "pnl": 0.012, "status": "Open", "outcome": "Open", "day": 4,
            "max_days": 20, "progress": 0.3, "following": True, "reason": None}]
    md = I.book_markdown([dict(upd[0], thesis_status="Needs a tweak", thesis_note="Catalyst moved to January.")], upd, "abc")
    assert "| ⭐ Long JPM / short BAC | 4 of 20 |" in md and "Needs a tweak" in md and "Catalyst moved" in md


def test_memory_track_record(db, fake_prices, settings):
    from datetime import date
    from market_updater.analysis import memory as MEM
    fake_prices.load(["NVDA", "SMH", "AMD"])
    claims = [{"id": i, "tickers": ["NVDA"], "stance": "bull", "kind": "opinion", "speaker": "Vik Sekar",
               "source_name": "Vik's Newsletter", "claim": "Nvidia's HBM supply is the bottleneck"} for i in range(1, 7)]
    claims.append({"id": 99, "tickers": ["NVDA", "AMD"], "stance": "bull", "kind": "opinion", "speaker": "X"})
    assert MEM.record_calls(settings, db, claims, fake_prices, date(2026, 11, 20)) == 6
    old = str(fake_prices.frame.index[-60].date())
    db.ex("UPDATE calls SET date=?", (old,))
    assert MEM.score_calls(db, fake_prices) == 6
    tr = MEM.track_record(db)
    assert tr and tr[0]["voice"] == "Vik Sekar" and tr[0]["n"] == 6
    assert "Vik Sekar" in MEM.context(db, date(2026, 11, 20)) and "| Vik Sekar | 6 |" in MEM.scorecard_markdown(db)
