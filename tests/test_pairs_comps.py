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
