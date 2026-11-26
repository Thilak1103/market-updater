"""The daily pitch: one long or short in a single stock, any market, 6-12 months.

Built to the standard Thilak signed off on with the LSEG sample (24 Nov 2026):
- think like an investor: a variant view, what is already priced in (consensus and a reverse DCF),
  three real theses, catalysts written as dated tests, honest risks and kill criteria;
- a plain-English business and industry overview;
- a short version in the briefing, with "Read the full pitch" opening the full page in the
  trade idea dashboard.

Steps: Opus proposes candidates from the day's material -> code checks they trade and are big
enough -> Opus researches the best one on the web -> code builds the data pack and the reverse
DCF -> Opus writes the pitch -> a sceptical Opus reviewer scores it 1-10 and the writer gets one
rewrite. The pitch runs even if it scores below the bar, with the reviewer's pushback shown.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..analysis.checks import CITE, check_section, tidy
from ..db import DB
from ..llm import LLMUnavailable
from ..market import comps as C
from ..market import fundamentals as F
from ..market import positioning as POS
from ..settings import Settings
from ..util import log
from . import prompts as P
from .sections import claim_line

SECTORS = ["TMT", "Autos", "Staples and retail", "Financials", "Macro", "Elsewhere"]
MARKETS = ["US", "UK", "Europe", "Japan", "Asia", "Other"]

INVESTOR = """How a good long/short investor thinks (apply all of it):
- A pitch is a VARIANT VIEW: say what the market believes, why, and why it is wrong. "It's cheap" or
  "it's a great company" is not a pitch.
- Ask what is already PRICED IN: use consensus numbers and the reverse DCF you are given. If our
  view is already in consensus, there is no trade.
- The business needs a reason to win (for a long) or a structural problem the market underrates
  (for a short). Face the obvious objection head on: e.g. no one wants European autos without an
  answer to Chinese competition; no one will short a compounder without a reason it breaks.
- Catalysts are DATED TESTS inside the horizon: an event, a date, and the number or decision we
  need to see. Never "revenue grows in Q1" or "sentiment improves".
- Management cutting guidance is bad news; buyback step-downs, expensive deals and adjusted vs
  reported earnings gaps matter. Say so.
- Theses are arguments with evidence and numbers, not buzzwords ("AI tailwinds", "best-in-class",
  "well-positioned"). Each thesis should be something a sceptical PM could check.
"""

CANDIDATES = P.STYLE + INVESTOR + """
You are a long/short equity PM choosing ONE stock to pitch today for a 6 to 12 month holding period.
Any listed stock in any market (US, UK, Europe, Japan, Asia, emerging markets), long or short, with a
market value above $2bn and enough liquidity to trade. It need not be in the material: use the
material as leads, and add names you know have a live debate, but every candidate needs a reason to
look at it NOW.

Return JSON with 5 candidates, best first:
- ticker: the Yahoo Finance symbol (e.g. LSEG.L, ASML.AS, 7203.T, 0700.HK, MC.PA, NVDA).
- name, direction ("Long" or "Short"), sector (one of: {sectors}), market (one of: {markets}).
- variant_view: one sentence, what the market believes vs what we believe.
- why_now: the dated event inside 12 months that tests it.
- peers: 3 or 4 Yahoo tickers of the closest listed competitors.
- cites: claim ids that support it (may be empty).
Never propose these (pitched recently or still open): {blocked}.
"""

CANDIDATES_SCHEMA = {"type": "object", "properties": {"candidates": {"type": "array", "items": {
    "type": "object", "properties": {
        "ticker": {"type": "string"}, "name": {"type": "string"},
        "direction": {"type": "string", "enum": ["Long", "Short"]},
        "sector": {"type": "string", "enum": SECTORS}, "market": {"type": "string", "enum": MARKETS},
        "variant_view": {"type": "string"}, "why_now": {"type": "string"},
        "peers": {"type": "array", "items": {"type": "string"}},
        "cites": {"type": "array", "items": {"type": "integer"}}},
    "required": ["ticker", "name", "direction", "sector", "market", "variant_view", "why_now", "peers", "cites"]}}},
    "required": ["candidates"]}

RESEARCH = """You are a buy-side analyst preparing a research dossier for a {direction} pitch on {name}
({ticker}) with a 6 to 12 month horizon. Today is {today}. Search the web (company results releases
and investor pages, regulatory filings, reputable financial press) and write a dossier with these
sections, in plain text:

1. Business: what the company sells, by segment, with each segment's revenue and share of the total
   and its recent growth; who the customers are and how they pay; one concrete example per segment.
2. Industry and competitors: how the industry works, who competes in each segment, market shares
   where known, and the main debate about the industry.
3. Recent results: the last two to four reported periods against consensus, and every guidance change.
4. Consensus now: revenue and EPS forecasts for this year and next, the average price target, the
   number of buy/hold/sell ratings, and recent estimate revisions.
5. Valuation against the closest peers.
6. Capital allocation: buybacks, dividends, acquisitions (with prices paid), debt, activists.
7. Dated events in the next 12 months: results dates, investor days, regulatory decisions, deal
   closings, lock-ups, index changes, AGMs.
8. The bull and bear arguments, attributed to named people or firms.
9. Risks.

Every fact needs its number, its date and its source as a markdown link [publisher](url). If you
cannot find something, write "not found"; never guess. No preamble."""

WRITE = P.STYLE + INVESTOR + """
Write the full pitch for {direction} {name} ({ticker}). Entry price: {entry} {unit}. Horizon: 6 to 12
months, to {horizon}. You are given a research dossier (with source links), a data pack (prices,
valuation, consensus, positioning, comps) and a reverse DCF worked out in code.

Citations: cite web facts with the markdown link from the dossier, e.g. [Reuters](https://...), and
claims from the material with their id, e.g. [c123]. Use only numbers from the dossier, the data pack
or the reverse DCF; mark anything calculated from them "(our calculation)". All prices in {unit}, the
same units as the entry price.

Return JSON:
- headline: the whole idea in one sentence of at most 20 words, no numbers.
- summary: two or three sentences: the variant view, why now, and what the price assumes.
- business: plain_english (three to five sentences a student with no finance job could follow: what
  it is and how it makes money) and segments (each: name, what it sells with an example, who pays and
  how, share of revenue, recent growth).
- industry: how_it_works (two to four sentences), competitors (each: area and the main names), and
  debate (one sentence: the argument investors are having about this industry).
- market_thinks and we_think: two to four sentences each.
- priced_in: three or four bullets: consensus numbers, what the reverse DCF says the price assumes,
  and how that compares with our view.
- where_we_differ: two or three bullets, each a specific number or event where we expect consensus
  to be wrong and by how much.
- theses: exactly three, each with a title (a claim, not a label) and two or three evidence points.
- catalysts: three to five, all before {horizon}, each with date (e.g. "4 Feb 2027" or "late Feb 2027"),
  event, and need_to_see (the specific result or decision that confirms the thesis).
- risks: three to five, each with risk, why_it_matters and what_we_watch. Include the strongest bear
  argument.
- wrong_if: two to four kill criteria, each specific and checkable.
- base, bull, bear: each with price (a number in {unit}) and how (one sentence on the method, e.g. the
  DCF case or the multiple used). For a short, the bear case is where the trade loses.
- why_now: two sentences.
- conviction: High, Medium or Low, and conviction_reason (one sentence).
"""

PITCH_SCHEMA = {"type": "object", "properties": {
    "headline": {"type": "string"}, "summary": {"type": "string"},
    "business": {"type": "object", "properties": {
        "plain_english": {"type": "string"},
        "segments": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "what": {"type": "string"}, "who_pays": {"type": "string"},
            "share": {"type": "string"}, "growth": {"type": "string"}},
            "required": ["name", "what", "who_pays", "share", "growth"]}}},
        "required": ["plain_english", "segments"]},
    "industry": {"type": "object", "properties": {
        "how_it_works": {"type": "string"}, "debate": {"type": "string"},
        "competitors": {"type": "array", "items": {"type": "object", "properties": {
            "area": {"type": "string"}, "names": {"type": "string"}}, "required": ["area", "names"]}}},
        "required": ["how_it_works", "debate", "competitors"]},
    "market_thinks": {"type": "string"}, "we_think": {"type": "string"},
    "priced_in": {"type": "array", "items": {"type": "string"}},
    "where_we_differ": {"type": "array", "items": {"type": "string"}},
    "theses": {"type": "array", "items": {"type": "object", "properties": {
        "title": {"type": "string"}, "points": {"type": "array", "items": {"type": "string"}}},
        "required": ["title", "points"]}},
    "catalysts": {"type": "array", "items": {"type": "object", "properties": {
        "date": {"type": "string"}, "event": {"type": "string"}, "need_to_see": {"type": "string"}},
        "required": ["date", "event", "need_to_see"]}},
    "risks": {"type": "array", "items": {"type": "object", "properties": {
        "risk": {"type": "string"}, "why_it_matters": {"type": "string"}, "what_we_watch": {"type": "string"}},
        "required": ["risk", "why_it_matters", "what_we_watch"]}},
    "wrong_if": {"type": "array", "items": {"type": "string"}},
    "base": {"type": "object", "properties": {"price": {"type": "number"}, "how": {"type": "string"}},
             "required": ["price", "how"]},
    "bull": {"type": "object", "properties": {"price": {"type": "number"}, "how": {"type": "string"}},
             "required": ["price", "how"]},
    "bear": {"type": "object", "properties": {"price": {"type": "number"}, "how": {"type": "string"}},
             "required": ["price", "how"]},
    "why_now": {"type": "string"},
    "conviction": {"type": "string", "enum": ["High", "Medium", "Low"]}, "conviction_reason": {"type": "string"},
}, "required": ["headline", "summary", "business", "industry", "market_thinks", "we_think", "priced_in",
                "where_we_differ", "theses", "catalysts", "risks", "wrong_if", "base", "bull", "bear",
                "why_now", "conviction", "conviction_reason"]}

REVIEW = INVESTOR + """
You are the sceptical head of research. Score this {direction} pitch on {name} from 1 to 10 against
the standard above. Mark it down hard for: no real variant view; a "priced in" section that ignores
consensus or misreads the reverse DCF; theses that are buzzwords or unsupported; catalysts that are
not dated tests inside the horizon; risks that skip the strongest bear case, guidance or capital
allocation; ignoring a structural threat to the business; targets that don't follow from the
valuation; numbers that look invented. A 7 is a pitch you would take to an investment committee.
Return JSON: score (1-10), critique (at most 60 words: the biggest weakness), fixes (up to five
concrete changes)."""

REVIEW_SCHEMA = {"type": "object", "properties": {
    "score": {"type": "integer"}, "critique": {"type": "string"},
    "fixes": {"type": "array", "items": {"type": "string"}}}, "required": ["score", "critique", "fixes"]}

CHECK = P.STYLE + """
You check the open single-stock pitches in a research book each morning. For each you get the
thesis, the kill criteria, the next catalysts, the return so far and any new material on the stock.
For each return:
- status: "On track" (evidence and price both moving our way), "Watch" (nothing decisive, or price and
  evidence disagree), "Needs a tweak" (the thesis holds but a target, date or assumption should
  change), or "Broken" (a kill criterion has been met).
- note: at most 25 words on what changed and why it matters, with citations. If nothing new came
  out, judge from the price and say so plainly.
- tweak: at most 20 words on what to change, or "".
Return JSON: {"rows": [{"id": <id as given>, "status": "...", "note": "...", "tweak": "..."}]}."""


@dataclass
class Pitch:
    ticker: str
    name: str
    direction: str
    sector: str
    market: str
    currency: str
    unit: str
    entry: float
    horizon_end: str
    spec: dict = field(default_factory=dict)       # the written pitch
    data: dict = field(default_factory=dict)       # data pack pieces
    sources: list[dict] = field(default_factory=list)
    score: int = 0
    critique: str = ""
    cited: set[int] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)
    page_id: str | None = None
    markdown: str = ""        # the short version for the briefing
    full: str = ""            # the full pitch page

    @property
    def sign(self) -> int:
        return 1 if self.direction == "Long" else -1


# -- data ----------------------------------------------------------------------------

def _unit(currency: str | None) -> str:
    return {"GBp": "p", "GBX": "p", "ILA": "agorot", "ZAc": "c"}.get(currency or "", currency or "")


def fmt_price(v: float | None, unit: str) -> str:
    if v is None:
        return "–"
    s = f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.2f}"
    if unit == "p":
        return f"{s}p"
    sym = {"USD": "$", "GBP": "£", "EUR": "€", "JPY": "¥", "HKD": "HK$", "CHF": "CHF ", "INR": "₹"}.get(unit)
    return f"{sym}{s}" if sym else f"{s} {unit}".strip()


def yahoo_info(ticker: str) -> dict:
    import yfinance as yf
    try:
        return yf.Ticker(ticker).info or {}
    except Exception as e:
        log.debug("yfinance info for %s failed: %s", ticker, e)
        return {}


def fcf_yield(info: dict) -> float | None:
    """Free cash flow / market value, with a sanity check for pence-vs-pounds style mismatches."""
    fcf, mcap = info.get("freeCashflow"), info.get("marketCap")
    if not fcf or not mcap or fcf <= 0 or mcap <= 0:
        return None
    y = fcf / mcap
    for adj in (1, 100, 0.01):
        if 0.002 <= y * adj <= 0.25:
            return y * adj
    return None


def dcf_multiple(g: float, k: float, years: int, g_term: float = 0.025) -> float:
    """Value / this year's FCF: `years` of growth at g, then g_term forever, discounted at k."""
    v, f = 0.0, 1.0
    for t in range(1, years + 1):
        f *= 1 + g
        v += f / (1 + k) ** t
    return v + f * (1 + g_term) / (k - g_term) / (1 + k) ** years


def reverse_dcf(fy: float | None, price: float, unit: str) -> tuple[str, dict]:
    """Markdown table plus numbers: implied years of growth and 5-year DCF values per share."""
    if not fy:
        return ("Reverse DCF: not meaningful (free cash flow is negative or unavailable); use the "
                "multiples and peers instead.", {})
    mult = 1 / fy
    lines = [f"Free-cash-flow yield {fy * 100:.1f}% (price is {mult:.1f}x free cash flow). Terminal growth 2.5%.",
             "| FCF growth | Cost of equity | Years of that growth the price implies | Value per share with 5 years of it |",
             "|---|---|---|---|"]
    out = {"fcf_yield": fy, "rows": []}
    for g in (0.04, 0.08, 0.12):
        for k in (0.085, 0.095):
            n = next((n for n in range(0, 21) if dcf_multiple(g, k, n) >= mult), None)
            val = price * dcf_multiple(g, k, 5) / mult
            out["rows"].append({"g": g, "k": k, "years": n, "value5": val})
            lines.append(f"| {g * 100:.0f}% | {k * 100:.1f}% | {'more than 20' if n is None else n} | "
                         f"{fmt_price(val, unit)} ({(val / price - 1) * 100:+.0f}%) |".replace("(-", "(−"))
    return "\n".join(lines), out


def data_pack(db: DB, ticker: str, peers: list[str], today: date, info: dict, unit: str,
              price: float, history) -> tuple[str, dict]:
    pos = POS.get(db, ticker, today)
    comps = C.build([ticker], peers[:4], lambda t: F.get(db, t))
    rdcf_md, rdcf = reverse_dcf(fcf_yield(info), price, unit)
    s = history.dropna() if history is not None else None
    perf = ""
    if s is not None and len(s) > 30:
        perf = (f"1-year return {(s.iloc[-1] / s.iloc[0] - 1) * 100:+.0f}%, 1-year range "
                f"{fmt_price(float(s.min()), unit)} to {fmt_price(float(s.max()), unit)}.")
    trend = (pos.get("eps_trend") or {})
    rev = []
    for p, lab in (("0y", "this year"), ("+1y", "next year")):
        pts = trend.get(p)
        if pts and pts[0] and pts[-1]:
            rev.append(f"consensus EPS {lab} {pts[-1]:.2f}, {(pts[-1] / pts[0] - 1) * 100:+.1f}% in 90 days")
    bits = {
        "Price": f"{fmt_price(price, unit)}. {perf}",
        "Market value": (f"{C._fmt(info['marketCap'], 'money')} {info.get('financialCurrency') or ''}".strip()
                         if info.get("marketCap") else "–"),
        "Valuation": ", ".join(f"{lab} {info[k]:.1f}x" for k, lab in (
            ("trailingPE", "P/E LTM"), ("forwardPE", "P/E forward"), ("enterpriseToEbitda", "EV/EBITDA"),
            ("enterpriseToRevenue", "EV/Sales")) if isinstance(info.get(k), (int, float))) or "–",
        "Dividend yield": f"{info['dividendYield']:.1f}%" if isinstance(info.get("dividendYield"), (int, float)) else "–",
        "Street": (f"average target {fmt_price(pos.get('target_mean'), unit)}"
                   + (f" from {pos['n_analysts']} analysts" if pos.get("n_analysts") else "")
                   + f"; {POS._ratings(pos)}") if pos.get("target_mean") else POS._ratings(pos),
        "Estimate revisions": "; ".join(rev) or "–",
        "Short interest": f"{pos['short_pct_float'] * 100:.1f}% of float" if pos.get("short_pct_float") else "–",
        "Next results": pos.get("next_earnings") or "–",
    }
    md = "\n".join(f"- {k}: {v}" for k, v in bits.items())
    md += f"\n\nReverse DCF (our calculation):\n{rdcf_md}\n\nComps:\n{comps['markdown']}"
    return md, {"positioning": pos, "comps": comps, "rdcf": rdcf, "rdcf_md": rdcf_md, "bits": bits}


# -- checks ------------------------------------------------------------------------

LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def checked(text: str, claims_by_id: dict[int, dict], allowed_texts: list[str], numbers: set[str]) -> tuple[str, set[int], list[str]]:
    """Number and citation checks that leave markdown links alone (URLs are full of digits)."""
    links: list[str] = []

    def stash(m):
        links.append(m.group(0))
        return f"⟦{len(links) - 1}⟧"
    body = LINK.sub(stash, text or "")
    out, problems = check_section(body, claims_by_id, numbers, allowed_texts)
    out = re.sub(r"⟦(\d+)⟧", lambda m: links[int(m.group(1))], out)
    out = tidy(out)
    return out, {int(x) for x in CITE.findall(out)}, problems


# -- the pipeline ------------------------------------------------------------------

def recently_pitched(db: DB, today: date, days: int) -> set[str]:
    since = (today - timedelta(days=days)).isoformat()
    return {r["ticker"] for r in db.q("SELECT ticker FROM pitches WHERE created>=? OR status='Open'", (since,))}


def _material(settings: Settings, claims: list[dict], tickers: set[str] | None = None, limit: int = 60) -> tuple[str, dict]:
    from .sections import select
    pool = [c for c in claims if tickers & set(c.get("tickers") or [])] if tickers else select(claims, settings.people, limit=limit)
    pool = pool[:limit]
    return "\n".join(claim_line(c, settings.people) for c in pool), {c["id"]: c for c in pool}


def _horizon(today: date, months: int) -> str:
    y, m = divmod(today.month - 1 + months, 12)
    return date(today.year + y, m + 1, min(today.day, 28)).isoformat()


def generate(settings: Settings, db: DB, llm, prices, states, claims: list[dict], reports, snap,
             today: date | None = None, info_fn=None) -> Pitch | None:
    today = today or date.today()
    cfg = settings.get("pitch", {}) or {}
    blocked = recently_pitched(db, today, int(cfg.get("no_repeat_days", 60)))
    from .ideas import _context
    ctx, ctx_claims = _context(settings, states, claims, reports, snap, db, today)
    try:
        res = llm.json(tier="trade_ideas", purpose="pitch_candidates",
                       system=CANDIDATES.format(sectors=", ".join(SECTORS), markets=", ".join(MARKETS),
                                                blocked=sorted(blocked) or "none"),
                       user=f"Today is {today.isoformat()}.\n\n{ctx}", schema=CANDIDATES_SCHEMA, max_tokens=4000)
    except (LLMUnavailable, ValueError) as e:
        log.warning("Pitch candidates failed: %s", e)
        return None
    min_cap = float(cfg.get("min_market_cap_usd", 2e9))
    tries = int(cfg.get("candidates_researched", 2))
    best: Pitch | None = None
    for cand in res.get("candidates", []):
        if tries <= 0:
            break
        t = (cand.get("ticker") or "").strip().upper().replace(" ", "")
        if not t or t in blocked:
            continue
        hist = prices.fetch([t], "1y")
        if hist is None or hist.empty or t not in hist.columns or hist[t].dropna().empty:
            log.info("Pitch candidate %s has no prices; skipped", t)
            continue
        info = (info_fn or yahoo_info)(t)
        cap_usd = (info.get("marketCap") or 0) * {"USD": 1}.get(info.get("currency", "USD"), 1)
        if info.get("marketCap") and info.get("currency") == "USD" and cap_usd < min_cap:
            log.info("Pitch candidate %s is too small (%s)", t, info.get("marketCap"))
            continue
        tries -= 1
        p = _build(settings, db, llm, cand, t, hist[t], info, claims, ctx_claims, snap, today, cfg)
        if p and (best is None or p.score > best.score):
            best = p
        if best and best.score >= int(cfg.get("min_review_score", 7)):
            break
    return best


def _build(settings, db, llm, cand, ticker, history, info, claims, ctx_claims, snap, today, cfg) -> Pitch | None:
    price = float(history.dropna().iloc[-1])
    unit = _unit(info.get("currency"))
    p = Pitch(ticker=ticker, name=cand.get("name") or info.get("shortName") or ticker,
              direction=cand.get("direction", "Long"), sector=cand.get("sector", "Elsewhere"),
              market=cand.get("market", "Other"), currency=info.get("currency") or "", unit=unit, entry=price,
              horizon_end=_horizon(today, int(cfg.get("horizon_months", 12))))
    pack, p.data = data_pack(db, ticker, cand.get("peers") or [], today, info, unit, price, history)
    try:
        dossier, p.sources = llm.research(
            tier="trade_ideas", purpose="pitch_research",
            system=RESEARCH.format(direction=p.direction.lower(), name=p.name, ticker=ticker, today=today.isoformat()),
            user=f"Starting view: {cand.get('variant_view')}\nWhy now: {cand.get('why_now')}\n\nData pack:\n{pack}",
            max_searches=int(cfg.get("max_searches", 12)))
    except (LLMUnavailable, ValueError) as e:
        log.warning("Research for %s failed: %s", ticker, e)
        dossier = "(web research unavailable today: rely on the data pack and material)"
    material, by_id = _material(settings, claims, {ticker})
    cited = [ctx_claims[i] for i in cand.get("cites") or [] if i in ctx_claims]
    by_id.update({c["id"]: c for c in cited})
    material += "\n" + "\n".join(claim_line(c, settings.people) for c in cited)
    p.data["dossier"] = dossier
    system = WRITE.format(direction=p.direction.lower(), name=p.name, ticker=ticker, entry=fmt_price(price, unit),
                          unit=unit or "local currency", horizon=p.horizon_end)
    user = (f"Starting view: {cand.get('variant_view')}\nWhy now: {cand.get('why_now')}\n\n"
            f"RESEARCH DOSSIER\n{dossier}\n\nDATA PACK\n{pack}\n\nMATERIAL\n{material or '(none)'}")
    spec = _ask(llm, "pitch_write", system, user, PITCH_SCHEMA, 9000)
    if not spec:
        return None
    review = _ask(llm, "pitch_review", REVIEW.format(direction=p.direction.lower(), name=p.name),
                  json.dumps(spec, ensure_ascii=False) + f"\n\nDATA PACK\n{pack}", REVIEW_SCHEMA, 1500) or {}
    if int(review.get("score") or 0) < int(cfg.get("min_review_score", 7)) and review.get("fixes"):
        again = _ask(llm, "pitch_rewrite", system, user + "\n\nA sceptical reviewer scored your first draft "
                     f"{review.get('score')}/10: {review.get('critique')}\nFix these: {review.get('fixes')}\n\n"
                     f"First draft:\n{json.dumps(spec, ensure_ascii=False)}", PITCH_SCHEMA, 9000)
        if again:
            spec = again
            review = _ask(llm, "pitch_review", REVIEW.format(direction=p.direction.lower(), name=p.name),
                          json.dumps(spec, ensure_ascii=False) + f"\n\nDATA PACK\n{pack}", REVIEW_SCHEMA, 1500) or review
    p.score, p.critique = int(review.get("score") or 0), review.get("critique", "")
    p.spec = _clean_spec(spec, by_id, [dossier, pack, p.data.get("rdcf_md", "")], set(snap.numbers), p)
    return p


def _ask(llm, purpose, system, user, schema, max_tokens) -> dict | None:
    try:
        return llm.json(tier="trade_ideas", purpose=purpose, system=system, user=user, schema=schema,
                        max_tokens=max_tokens)
    except (LLMUnavailable, ValueError) as e:
        log.warning("%s failed: %s", purpose, e)
        return None


def _clean_spec(spec: dict, by_id: dict, texts: list[str], numbers: set[str], p: Pitch) -> dict:
    def fix(v):
        if isinstance(v, str):
            out, ids, probs = checked(v, by_id, texts, numbers)
            p.cited |= ids
            p.problems += probs
            return out
        if isinstance(v, list):
            return [x for x in (fix(i) for i in v) if x not in ("", None, [], {})]
        if isinstance(v, dict):
            return {k: (fix(x) if k not in ("price",) else x) for k, x in v.items()}
        return v
    return fix(spec)


# -- rendering ---------------------------------------------------------------------

def _pct(target: float | None, p: Pitch) -> str:
    """Return on the position if the price reaches target (a short gains when the price falls)."""
    if not target or not p.entry:
        return ""
    return f"{(target / p.entry - 1) * 100 * p.sign:+.0f}%".replace("-", "−")


def short_markdown(p: Pitch, page_link: str | None, best_of_day: bool = False) -> str:
    s = p.spec
    long = p.direction == "Long"
    icon, colour = ("📈", "green") if long else ("📉", "red")
    targets = " · ".join(f"{lab} {fmt_price(s[k]['price'], p.unit)} ({_pct(s[k]['price'], p)})"
                         for k, lab in (("base", "**Base**"), ("bull", "Bull"), ("bear", "Bear")) if s.get(k))
    lines = [f"::: callout {icon} {colour}",
             f"**{p.direction.upper()} {p.name} ({p.ticker})** · 6–12 months · entry {fmt_price(p.entry, p.unit)}"
             + (" · returns shown for the short" if p.direction == "Short" else ""),
             f"*{s.get('headline', '').rstrip('.')}.*",
             targets + (f" · Reviewer {p.score}/10" if p.score else ""),
             s.get("summary", "")]
    if s.get("theses"):
        lines.append("**Why:** " + " ".join(f"{i}. {t['title'].rstrip('.')}." for i, t in enumerate(s["theses"][:3], 1)))
    if s.get("priced_in"):
        lines.append(f"**What's priced in:** {s['priced_in'][0]}")
    if s.get("catalysts"):
        c = s["catalysts"][0]
        lines.append(f"**Next test:** {c['date']}, {c['event'].rstrip('.')}: {c['need_to_see'].rstrip('.')}.")
    if s.get("risks"):
        r = s["risks"][0]
        lines.append(f"**Biggest risk:** {r['risk'].rstrip('.')}. {r['why_it_matters'].rstrip('.')}.")
    if best_of_day and p.critique:
        lines.append(f"**Where the reviewer pushed back:** {p.critique}")
    more = f"[Read the full pitch →](notion:{page_link})" if page_link else "Full pitch in the pitch dashboard"
    lines.append(f"{more} · Tick **Following** there to track it.")
    lines.append(":::")
    return "\n".join(l for l in lines if l is not None and l != "")


def full_markdown(p: Pitch, today: date, best_of_day: bool = False) -> str:
    s, d = p.spec, p.data
    long = p.direction == "Long"
    icon, colour = ("📈", "green_bg") if long else ("📉", "red_bg")
    out = [f"::: callout 🧪 gray_bg",
           f"Research, not advice. Prices as of {today.strftime('%-d %b %Y')}. Our own calculations are labelled. "
           f"Reviewer score {p.score}/10" + (f": {p.critique}" if p.critique else "."),
           ":::",
           f"::: callout {icon} {colour}",
           f"**{p.direction.upper()} {p.name} ({p.ticker}) · 6–12 months (to {date.fromisoformat(p.horizon_end).strftime('%b %Y')}) · "
           f"Entry {fmt_price(p.entry, p.unit)}**" + (" · returns shown for the short" if p.direction == "Short" else ""),
           " · ".join(f"**{lab}** {fmt_price(s[k]['price'], p.unit)} ({_pct(s[k]['price'], p)})"
                      for k, lab in (("base", "Base"), ("bull", "Bull"), ("bear", "Bear")) if s.get(k)),
           s.get("summary", ""),
           ":::"]
    if best_of_day and p.critique:
        out += [f"> **Where the reviewer pushed back:** {p.critique}"]
    b = s.get("business") or {}
    out += ["## What the company does, in plain English", b.get("plain_english", "")]
    if b.get("segments"):
        out += ["| Segment | What it sells | Who pays, and how | Share of revenue | Recent growth |",
                "|---|---|---|---|---|"]
        out += [f"| **{g['name']}** | {g['what']} | {g['who_pays']} | {g['share']} | {g['growth']} |" for g in b["segments"]]
    ind = s.get("industry") or {}
    out += ["## The industry, and who it competes with", ind.get("how_it_works", "")]
    if ind.get("competitors"):
        out += ["| Area | Main competitors |", "|---|---|"]
        out += [f"| {c['area']} | {c['names']} |" for c in ind["competitors"]]
    if ind.get("debate"):
        out.append(f"**The debate in one line:** {ind['debate']}")
    out += ["## The set-up", "| | |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in (d.get("bits") or {}).items()]
    out += ["## What the market thinks vs what we think",
            f"**The market thinks:** {s.get('market_thinks', '')}", f"**We think:** {s.get('we_think', '')}",
            "## What's already priced in"]
    out += [f"- {x}" for x in s.get("priced_in", [])]
    if d.get("rdcf_md"):
        out += ["::: toggle The reverse DCF (our calculation)", d["rdcf_md"], ":::"]
    if s.get("where_we_differ"):
        out += ["**Where we differ from consensus:**"] + [f"{i}. {x}" for i, x in enumerate(s["where_we_differ"], 1)]
    out.append("## Three reasons it works")
    for i, t in enumerate(s.get("theses", [])[:3], 1):
        out += [f"### {i}. {t['title']}"] + [f"- {x}" for x in t.get("points", [])]
    out += ["## Catalysts: what has to happen, and when", "| When | Event | What we need to see |", "|---|---|---|"]
    out += [f"| {c['date']} | {c['event']} | {c['need_to_see']} |" for c in s.get("catalysts", [])]
    out += ["## Risks", "| Risk | Why it matters | What we watch |", "|---|---|---|"]
    out += [f"| {r['risk']} | {r['why_it_matters']} | {r['what_we_watch']} |" for r in s.get("risks", [])]
    out += ["## What would prove us wrong"] + [f"- {x}" for x in s.get("wrong_if", [])]
    out.append("## Valuation")
    for k, lab in (("base", "Base"), ("bull", "Bull"), ("bear", "Bear")):
        if s.get(k):
            out.append(f"- **{lab} {fmt_price(s[k]['price'], p.unit)} ({_pct(s[k]['price'], p)}):** {s[k]['how']}")
    out += ["## Why now", s.get("why_now", ""),
            f"*Conviction: {s.get('conviction', '–')}. {s.get('conviction_reason', '')}*"]
    comps = (d.get("comps") or {}).get("markdown")
    if comps:
        out += ["::: toggle Comps", comps, ":::"]
    if p.sources:
        out += ["::: toggle Sources the research used"] + [f"- [{x['title']}]({x['url']})" for x in p.sources[:30]] + [":::"]
    return "\n".join(x for x in out if x is not None)


# -- storage and tracking ------------------------------------------------------------

def save(db: DB, p: Pitch, today: date) -> int:
    s = p.spec
    nxt = (s.get("catalysts") or [{}])[0]
    return db.ex(
        "INSERT INTO pitches(notion_page_id,created,ticker,name,direction,market,sector,currency,entry_price,base,bull,"
        "bear,horizon_end,headline,summary,conviction,score,critique,next_catalyst,next_catalyst_date,last_price,spec) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (p.page_id, today.isoformat(), p.ticker, p.name, p.direction, p.market, p.sector, p.currency, p.entry,
         (s.get("base") or {}).get("price"), (s.get("bull") or {}).get("price"), (s.get("bear") or {}).get("price"),
         p.horizon_end, s.get("headline"), s.get("summary"), s.get("conviction"), p.score, p.critique,
         f"{nxt.get('event', '')}: {nxt.get('need_to_see', '')}".strip(": "), nxt.get("date"), p.entry,
         json.dumps({**s, "unit": p.unit, "full": p.full}, ensure_ascii=False)))


def open_pitches(db: DB) -> list[dict]:
    return [dict(r) for r in db.q("SELECT * FROM pitches WHERE status='Open' ORDER BY created")]


def update_open(db: DB, prices, today: date) -> list[dict]:
    """Mark open pitches to market and close them at the base case, the bear case or the horizon."""
    out = []
    rows = open_pitches(db)
    if not rows:
        return out
    hist = prices.fetch(sorted({r["ticker"] for r in rows}), "1mo")
    for r in rows:
        if hist is None or r["ticker"] not in getattr(hist, "columns", []):
            continue
        s = hist[r["ticker"]].dropna()
        if s.empty:
            continue
        last = float(s.iloc[-1])
        sign = 1 if r["direction"] == "Long" else -1
        ret = (last / r["entry_price"] - 1) * sign
        best, worst = max(r["best"] or 0, ret), min(r["worst"] or 0, ret)
        status, outcome, reason = "Open", "Open", None
        base, bear = r["base"], r["bear"]
        if base and (last - base) * sign >= 0:
            status, outcome, reason = "Closed", "Hit base case", "the price reached the base-case target"
        elif bear and (last - bear) * sign <= 0:
            status, outcome, reason = "Stopped out", "Hit bear case", "the price reached the bear case"
        elif r["horizon_end"] and today.isoformat() >= r["horizon_end"]:
            status, outcome, reason = "Expired", "Expired up" if ret > 0 else "Expired down", "the 12-month horizon ended"
        db.ex("UPDATE pitches SET last_price=?, ret=?, best=?, worst=?, status=?, outcome=?, closed=?, close_reason=? "
              "WHERE id=?", (last, ret, best, worst, status, outcome, today.isoformat() if status != "Open" else None,
                             reason, r["id"]))
        held = (today - date.fromisoformat(r["created"][:10])).days
        to_base = ((base / r["entry_price"] - 1) * sign) if base else None
        out.append({**r, "last_price": last, "ret": ret, "status": status, "outcome": outcome, "reason": reason,
                    "held": held, "progress": (ret / to_base) if to_base else None,
                    "following": bool(r.get("following"))})
    return out


def thesis_check(llm, db: DB, settings: Settings, claims: list[dict], updates: list[dict], today: date, snap) -> list[dict]:
    live = [u for u in updates if u["status"] == "Open"]
    if not live:
        return []
    blocks, by_id = [], {}
    for u in live:
        spec = json.loads(u.get("spec") or "{}")
        news = [c for c in claims if u["ticker"] in set(c.get("tickers") or [])][:8]
        by_id.update({c["id"]: c for c in news})
        blocks.append("\n".join([
            f"Pitch {u['id']}: {u['direction']} {u['name']} ({u['ticker']}): {u.get('headline') or ''}",
            f"We think: {spec.get('we_think', '')}",
            f"Kill criteria: {spec.get('wrong_if', [])}",
            f"Next catalysts: {[(c.get('date'), c.get('event')) for c in spec.get('catalysts', [])[:3]]}",
            f"So far: {u['held']} days held, return {u['ret'] * 100:+.1f}%",
            "New material:", *([claim_line(c, settings.people) for c in news] or ["none today"])]))
    try:
        res = llm.json(tier="write", purpose="pitch_check", system=CHECK, user="\n\n".join(blocks),
                       schema=P.THESIS_SCHEMA, max_tokens=2500) or {}
    except (LLMUnavailable, ValueError) as e:
        log.warning("Pitch check failed: %s", e)
        return []
    from .sections import clean
    ids = {u["id"]: u for u in live}
    out = []
    for row in res.get("rows", []):
        u = ids.get(row.get("id"))
        if not u:
            continue
        note = clean(row.get("note", ""), by_id, snap)[0]
        tweak = clean(row.get("tweak", "") or "", by_id, snap)[0]
        status = row.get("status") if row.get("status") in ("On track", "Watch", "Needs a tweak", "Broken") else "Watch"
        full = note + (f" **Tweak:** {tweak}" if tweak else "")
        db.ex("UPDATE pitches SET thesis_status=?, thesis_note=?, thesis_checked=? WHERE id=?",
              (status, full, today.isoformat(), u["id"]))
        out.append({**u, "thesis_status": status, "thesis_note": full})
    return out


STATUS_COLOR = {"On track": "green", "Watch": "orange", "Needs a tweak": "yellow", "Broken": "red"}


def book_markdown(checks: list[dict], updates: list[dict], dashboard_id: str | None) -> str:
    rows = {c["id"]: c for c in checks}
    live = [rows.get(u["id"], u) for u in updates if u["following"]]
    link = f"[Pitch dashboard](notion:{dashboard_id})" if dashboard_id else "the pitch dashboard"
    if not live:
        n = sum(1 for u in updates if u["status"] == "Open")
        still = f" {n} pitch{'es are' if n != 1 else ' is'} open there." if n else ""
        return (f"*You aren't following any pitches yet. Tick **Following** on one in {link} and it will be "
                f"tracked here every morning.{still}*")
    lines = ["| Pitch | Days | Return | To base case | Thesis | What changed |", "|---|---|---|---|---|---|"]
    for u in sorted(live, key=lambda u: (u["status"] != "Open", -u["held"])):
        col = "green" if u["ret"] > 0 else "red" if u["ret"] < 0 else "gray"
        pnl = f"{u['ret'] * 100:+.1f}%".replace("-", "−")
        prog = f"{max(0, u['progress']) * 100:.0f}%" if u.get("progress") is not None else "–"
        if u["status"] != "Open":
            thesis, note = f"Closed: {u['outcome'].lower()}", u.get("reason") or ""
        else:
            st = u.get("thesis_status") or "–"
            thesis = f'<span color="{STATUS_COLOR.get(st, "gray")}">{st}</span>' if st != "–" else "–"
            note = u.get("thesis_note") or "Not checked today"
        lines.append(f"| {u['direction']} {u['name']} ({u['ticker']}) | {u['held']} | <span color=\"{col}\">{pnl}</span> | "
                     f"{prog} | {thesis} | {note} |")
    return "\n".join(lines) + f"\n*Every open pitch, followed or not, is tracked in {link}.*"


def scorecard(db: DB) -> str:
    closed = [dict(r) for r in db.q("SELECT * FROM pitches WHERE status!='Open'")]
    n_open = db.one("SELECT COUNT(*) n FROM pitches WHERE status='Open'")["n"]
    if not closed:
        return (f"No pitch has closed yet; {n_open} open. A pitch closes at its base case, its bear case, "
                "or after 12 months.")

    def col(rows):
        if not rows:
            return ["–"] * 4
        n = len(rows)
        return [str(sum(r["outcome"] == "Hit base case" for r in rows)), str(sum(r["outcome"] == "Hit bear case" for r in rows)),
                f"{sum((r['ret'] or 0) > 0 for r in rows) / n * 100:.0f}%",
                f"{sum(r['ret'] or 0 for r in rows) / n * 100:+.1f}%".replace("-", "−")]
    a, f = col(closed), col([r for r in closed if r.get("following")])
    labels = ["Hit base case", "Hit bear case", "Made money", "Average return"]
    lines = ["| | All pitches | Pitches you followed |", "|---|---|---|"]
    lines += [f"| {lab} | {x} | {y} |" for lab, x, y in zip(labels, a, f)]
    return "\n".join(lines) + f"\n\n*{len(closed)} closed, {n_open} open.*"
