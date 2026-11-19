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
        return synthetic_prices(tickers)
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
        return {"ideas": [
            {"long": "NVDA", "short": "AMD", "sector": "tmt", "origin": "Divergence",
             "headline": "Long Nvidia / short AMD: share gains not priced",
             "isolates": "Nvidia's lead in accelerators.", "thesis": "Capex is still rising [c1].",
             "why_now": "The crowd is bearish on capex returns [c1].",
             "consensus": "Most voices are bearish; this is against them.", "against_consensus": True,
             "wrong_if": "Hyperscalers cut capex guidance.", "confidence": "Medium",
             "confidence_reason": "Pair is well correlated.", "peers": ["AVGO", "TSM"]},
            {"long": "JPM", "short": "BAC", "sector": "financials", "origin": "Sector move",
             "headline": "Long JPMorgan / short Bank of America", "isolates": "Fee income versus rate sensitivity.",
             "thesis": "Rates are falling, which hurts the more rate-sensitive bank.", "why_now": "Rates rallied.",
             "consensus": "With consensus.", "against_consensus": False, "wrong_if": "Rates rise.",
             "confidence": "Low", "peers": ["GS", "MS", "NOTREAL"]},
            {"long": "NVDA", "short": "NVDA", "sector": "tmt", "origin": "Divergence", "headline": "Bad",
             "isolates": "", "thesis": "", "why_now": "", "consensus": "", "against_consensus": False,
             "wrong_if": "", "confidence": "Low", "peers": []},
        ]}
    if purpose.startswith("earnings_"):
        return {"verdict": "Shares moved more than usual; the call was better than that.",
                "reaction_fit": "overdone", "review": "- **Market reaction** Bigger than the usual move.\n- **Does the reaction fit?** No."}
    if purpose == "discovery_score":
        return {"scores": []}
    if purpose == "write_topline":
        return "- Capex worries are building [c1].\n- Made-up number 97.3% with no source."
    if purpose.startswith("write_"):
        return ("Voices are turning bearish on AI spending [c1]. A sentence citing a missing claim [c99999].\n"
                "- Something rose 12.34% today without any source.")
    raise AssertionError(f"unexpected purpose {purpose}")
