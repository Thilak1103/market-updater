"""Shared fixtures: an isolated database, the seed settings, synthetic prices, and a fake LLM."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from market_updater.db import DB
from market_updater.market.prices import Prices
from market_updater.settings import load_yaml


@pytest.fixture
def settings():
    return load_yaml()


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "test.db")


def synthetic_prices(tickers: list[str], days: int = 400, seed: int = 7) -> pd.DataFrame:
    """Random walks sharing a market factor, so pairs within a sector are correlated."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=pd.Timestamp("2026-11-19"), periods=days)
    market = rng.normal(0, 0.01, days)
    out = {}
    for i, t in enumerate(sorted(set(tickers))):
        own = rng.normal(0, 0.006, days)
        beta = 0.8 + (i % 5) * 0.1
        r = beta * market + own
        r[-1] += 0.03 if i % 4 == 0 else -0.01   # make some names move on the last day
        out[t] = 100 * np.exp(np.cumsum(r))
    return pd.DataFrame(out, index=idx)


@pytest.fixture
def fake_prices(db):
    def fetch(tickers, period="2y"):
        return synthetic_prices([t for t in tickers if "NOPRICE" not in t])   # NOPRICE: an unlisted name
    return Prices(db, fetch=fetch, cross_check=False)


def fake_responder(purpose: str, system: str, user: str):
    """Plausible model output for each call the pipeline makes."""
    if purpose == "extract_claims":
        items = re.findall(r'<item index="(\d+)"[^>]*>\n(.*?)\n</item>', user, re.S)
        claims = []
        for idx, text in items:
            words = text.split()
            quote = " ".join(words[:8])
            claims.append({"item": int(idx), "kind": "opinion", "speaker": "Torsten Slok", "via": "Apollo",
                           "sector": "tmt", "tickers": ["NVDA"], "debate": "ai-capex-roi", "stance": "bear",
                           "confidence": "medium", "claim": "AI capex is running ahead of revenue, up 40%.",
                           "evidence_quote": quote, "numbers": ["40%"]})
            claims.append({"item": int(idx), "kind": "fact", "speaker": "Reuters", "sector": "staples_retail",
                           "tickers": ["TSCO.L"], "debate": "", "stance": "", "confidence": "high",
                           "claim": "Tesco gained market share.", "evidence_quote": "this quote is not in the text",
                           "numbers": []})
        return {"claims": claims}
    if purpose == "trade_ideas":
        def idea(long, short, sector, **kw):
            base = {"long": long, "short": short, "long_name": f"{long} Corp", "short_name": f"{short} Corp",
                    "sector": sector, "origin": "Divergence", "headline": "Share gains are not priced",
                    "what_it_is": "A bet that the long gains share from the short.",
                    "long_case": "Capex is still rising [c1].", "short_case": "Its margins are under pressure.",
                    "wrong_if": "Hyperscalers cut capex guidance.", "against_consensus": True,
                    "conviction": "Medium", "conviction_reason": "the evidence is one-sided", "peers": ["AVGO", "TSM"]}
            base.update(kw)
            return base
        return {"ideas": [
            idea("NVDA", "AMD", "tmt", long_name="Nvidia", short_name="AMD"),
            idea("JPM", "BAC", "financials", against_consensus=False, conviction="Low",
                 long_case="Fee income rose 12.34% this quarter.", peers=["GS", "MS", "NOTREAL"]),
            idea("NVDA", "NVDA", "tmt", headline="Bad"),
            idea("AMD", "MU", "tmt"),                  # reuses a leg already taken: rejected
            idea("TSCO.L", "SBRY.L", "staples_retail", long_name="Tesco", short_name="Sainsbury's"),
            idea("GS", "MS", "financials", long_name="Goldman Sachs", short_name="Morgan Stanley",
                 peers=["JPM", "TSCO.L"]),
        ]}
    if purpose == "trade_idea_review":
        pitch = json.loads(user.split("The pitch:\n", 1)[1].split("\n", 1)[0])
        weak = pitch["long"] == "GS"
        return {"variant_consensus": "Analysts are mostly buyers of both names.",
                "variant_ours": "The market is underestimating how far the short leg's margins can fall.",
                "long_case": pitch["long_case"], "short_case": pitch["short_case"],
                "catalysts": [{"date": "2026-12-17", "event": "Results day"},
                              {"date": "2026-12-01", "event": "Invented catalyst with a made-up 44.4% number"}],
                "wrong_if": "Dropped idea" if weak else pitch["wrong_if"], "conviction": "Medium",
                "conviction_reason": "one-sided evidence", "score": 3 if weak else 7,
                "critique": "Thin evidence on the short leg."}
    if purpose.startswith("earnings_"):
        return {"verdict": "Shares moved more than usual; the call was better than that.",
                "reaction_fit": "overdone", "review": "- **Market reaction** Bigger than the usual move.\n- **Does the reaction fit?** No."}
    if purpose == "discovery_score":
        return {"scores": []}
    if purpose == "write_market_story":
        rows = re.findall(r"^- ([^:]+): ", user.split("Full market table")[0], re.M)
        return {"headline": "Risk-off day, led by the AI trade.", "mood": "risk_off",
                "bullets": [{"lead": "Why AI sold off", "text": "Capex worries are building [c1]."},
                            {"lead": "Made up", "text": "Made-up number 97.3% with no source."},
                            {"lead": "Meta", "text": "The claims supplied do not explain the move."}],
                "so_what": [{"name": r, "note": "worth watching"} for r in rows]}
    if purpose.startswith("write_") and purpose in ("write_tmt", "write_autos", "write_staples_retail",
                                                    "write_financials"):
        return {"blurb": "Voices are turning bearish on AI spending [c1]. A sentence citing a missing claim [c99999]. "
                         "Something rose 12.34% today without any source.",
                "reading": [{"item": 1, "summary": "Argues capex is outrunning revenue [c1]."}],
                "filtered": "generic stock-tip pieces"}
    if purpose == "write_elsewhere":
        return {"rows": [{"sector": "Energy", "why": "Oil supply news moved the group [c3]."}]}
    if purpose == "write_consensus":
        return {"rows": []}
    if purpose == "write_deep_dive":
        return ("**Supply news is driving energy.**\n### What happened\n- Oil supply news [c3].\n"
                "### Why it is happening\n- Producers are adding supply [c3].\n- The claims provided do not say more.\n"
                "### What to watch\n- The next OPEC meeting")
    if purpose.startswith("write_"):
        return "Voices are turning bearish on AI spending [c1]."
    if purpose == "pitch_candidates":
        return {"candidates": [
            {"ticker": "NOPRICE.XX", "name": "Ghost plc", "direction": "Long", "sector": "Elsewhere", "market": "UK",
             "variant_view": "x", "why_now": "x", "peers": [], "cites": []},
            {"ticker": "LSEG.L", "name": "London Stock Exchange Group", "direction": "Long", "sector": "Financials",
             "market": "UK", "variant_view": "The AI fear is aimed at a fifth of revenue.",
             "why_now": "February results and 2027 guidance.", "peers": ["ICE", "CME"], "cites": [1]}]}
    if purpose == "pitch_research":
        return ("Business: Data & Analytics is 44% of income [LSEG](https://www.lseg.com/results). "
                "Q3 income grew 8.8% against 7.8% expected [HL](https://www.hl.co.uk/lseg). Results on 26 Feb 2027.",
                [{"url": "https://www.lseg.com/results", "title": "LSEG results"}])
    if purpose in ("pitch_write", "pitch_rewrite"):
        good = purpose == "pitch_rewrite" or "Fix these" in user
        return {
            "headline": "Beat-and-raise data plumbing priced for no growth",
            "summary": "Q3 income grew 8.8% against 7.8% expected [HL](https://www.hl.co.uk/lseg). "
                       "The price assumes growth stops soon.",
            "business": {"plain_english": "LSEG sells market data and runs trading venues.",
                         "segments": [{"name": "Data & Analytics", "what": "Terminals and feeds",
                                       "who_pays": "Banks, by subscription", "share": "44%", "growth": "5%"}]},
            "industry": {"how_it_works": "Exchanges are natural monopolies.", "debate": "Does AI kill the terminal?",
                         "competitors": [{"area": "Terminals", "names": "Bloomberg, FactSet"}]},
            "market_thinks": "AI will hollow out the terminal.", "we_think": "Most revenue is plumbing.",
            "priced_in": ["The price implies only a few years of growth (our calculation)."],
            "where_we_differ": ["2027 growth beats consensus."],
            "theses": [{"title": "The AI fear is aimed at a fifth of the business", "points": ["Markets grew fastest."]},
                       {"title": "It is beating and raising", "points": ["Fee income rose 12.34% this quarter."]},
                       {"title": "An activist pushes the re-rating", "points": ["Elliott wants buybacks."]}],
            "catalysts": [{"date": "26 Feb 2027", "event": "FY results", "need_to_see": "2027 guidance above consensus"}],
            "risks": [{"risk": "Buybacks fall", "why_it_matters": "Returns drop sharply", "what_we_watch": "February update"}],
            "wrong_if": ["2027 guidance below consensus."],
            "base": {"price": 115.0, "how": "DCF base case"}, "bull": {"price": 125.0, "how": "Lower cost of equity"},
            "bear": {"price": 85.0, "how": "Lower multiple"},
            "why_now": "Results arrive inside the window.", "conviction": "High" if good else "Medium",
            "conviction_reason": "Evidence and timing line up."}
    if purpose == "pitch_review":
        rewritten = '"conviction": "High"' in user
        return {"score": 8 if rewritten else 5, "critique": "Catalysts too thin." if not rewritten else "Solid.",
                "fixes": [] if rewritten else ["Add more dated catalysts."]}
    if purpose == "pitch_check":
        return {"rows": []}
    raise AssertionError(f"unexpected purpose {purpose}")
