"""Prompt text for the writing steps. Kept in one place so it is easy to tune."""

STYLE = """House style:
- British English. Every sentence starts with a capital letter. Plain, specific, no hype.
- Keep facts and opinions apart: facts and data go under "News", views go under "Views".
- Cite every sentence that uses a claim with its id in square brackets, e.g. [c123].
- Write every view as: **Name** (outlet): the view [cN]. Never write "analysts think",
  "the market fears" or similar unless it summarises three or more cited, named claims.
- Use only numbers that appear in the cited claims or in the tables you are given.
  Never compute, round differently or invent a number. If unsure, leave the number out.
- Claims marked subscriber=true come from paid newsletters: paraphrase them and never
  quote more than a few words.
- A claim marked repeat=true repeats an earlier story: include it only if it adds something
  new, and start that bullet with "Update:".
- Mark a speaker whose stance changed with **changed view** and say what it was before.
"""

SECTOR = STYLE + """
You are writing the {sector} section of a daily buy-side briefing for a contrarian reader.
Sub-sectors: {subsectors}. About {words} words in total.

Return markdown with exactly these two sub-headings (no others, no tables):
### News and earnings
- bullets of facts and data only, most market-moving first
### Views and debates
- bullets of attributed opinions, forecasts and positions; lead with disagreement,
  crowded views and priority voices; note bull versus bear where both exist

If there is nothing worth saying under a heading, write "- Quiet day." under it.
"""

OVERVIEW = STYLE + """
You are writing the market overview narrative of a daily briefing. The market and macro
tables are already on the page; refer to them with [m] when you use their numbers.
About {words} words. Return markdown with exactly these sub-headings:
### What drove markets
- 3 or 4 bullets naming the narratives behind the moves, each tied to cited claims or [m]
### Central banks and data
- bullets on central-bank decisions, speeches and data releases (facts first, then views)
### Asia
- 1 or 2 bullets on Asian markets, which are still trading at {asof}
"""

ELSEWHERE = STYLE + """
Write the "Elsewhere" section: one or two lines per sector listed in the triggers, saying what
moved and why, using only the cited claims and [m] for table numbers. About {words} words.
Return only bullets, one sector per bullet, starting with the sector name in bold.
"""

DIVERGE = STYLE + """
Write "Where views diverge": two to four live debates from the data given. For each debate:
a bold one-line question, then bullets for the bull case and the bear case with named,
cited speakers, a line on where consensus sits (use the counts given; say "crowded" when
flagged), and any credible voice going against it, especially priority or contrarian ones.
Lead with crowded debates that have dissent, then cracking narratives, then changed views.
About {words} words. Markdown only.
"""

TOPLINE = STYLE + """
Write the "Top line": exactly three bullets drawn only from the drafted sections below,
keeping their citations. 1) What mattered most. 2) Where consensus sits. 3) The dissent or
cracking narrative worth knowing. Each bullet one or two sentences.
"""

IDEAS_SYSTEM = """You are a contrarian long/short equity analyst generating pair-trade ideas for a
student heading into a buy-side career. Ideas are for research, not advice.

Each idea is a pair: long one stock, short a peer or the sector proxy, so the trade isolates a
view rather than market direction. Use only tickers from the universe given. Prefer ideas that
(in order): exploit a crowded consensus with credible dissent; a narrative that is cracking;
an earnings reaction out of line with what the call said; a priority voice changing view; a
single-stock pitch from a newsletter or a short-seller report; a large sector move.
At least {min_against} idea(s) must go against a crowded consensus. Avoid pairs listed as open.
Ground every thesis in the cited claims [cN]; use only numbers from the claims or tables.
Return {n_candidates} candidates ranked best first; the code checks the statistics and keeps the
best {n_final}.
"""

IDEAS_SCHEMA = {
    "type": "object",
    "properties": {"ideas": {"type": "array", "items": {"type": "object", "properties": {
        "long": {"type": "string"}, "short": {"type": "string"},
        "sector": {"type": "string"},
        "origin": {"type": "string", "enum": ["Divergence", "Narrative cracking", "Earnings reaction",
                                              "Changed view", "Substack pitch", "Short-seller report",
                                              "Sector move"]},
        "headline": {"type": "string", "description": "e.g. 'Long Tesco / short Sainsbury's: share gains not priced'"},
        "isolates": {"type": "string", "description": "What view the pair isolates, one sentence"},
        "thesis": {"type": "string", "description": "2-3 sentences with [cN] citations"},
        "why_now": {"type": "string"},
        "consensus": {"type": "string", "description": "What the crowd thinks and whether this is with or against it"},
        "against_consensus": {"type": "boolean"},
        "wrong_if": {"type": "string", "description": "The event or data that would break the thesis"},
        "confidence": {"type": "string", "enum": ["Low", "Medium", "High"]},
        "confidence_reason": {"type": "string"},
        "peers": {"type": "array", "items": {"type": "string"}, "description": "3-4 comparable tickers from the universe"},
    }, "required": ["long", "short", "sector", "origin", "headline", "isolates", "thesis", "why_now",
                    "consensus", "against_consensus", "wrong_if", "confidence", "peers"]}}},
    "required": ["ideas"],
}

EARNINGS_SYSTEM = STYLE + """
You review one company's results for a buy-side reader. You get: reaction statistics computed in
code, estimates versus actuals where available, attributed reactions (claims), and the call
transcript with the previous call for comparison.

Return JSON with:
- verdict: one sentence, e.g. "Shares -8% vs a usual 4% move; the call was better than that."
- reaction_fit: "overdone", "about right" or "muted"
- review: markdown with bullets under these bold labels: **Market reaction**, **Numbers vs
  expectations**, **What people said**, **What the call said** (guidance changes; questions
  analysts pressed on and any management sidestepped; tone or wording shifts versus the previous
  call; metrics added or dropped), **Does the reaction fit?**
Only use numbers that appear in the inputs. Cite claims with [cN]; cite the transcript as [t].
"""

EARNINGS_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string"},
                   "reaction_fit": {"type": "string", "enum": ["overdone", "about right", "muted"]},
                   "review": {"type": "string"}},
    "required": ["verdict", "reaction_fit", "review"],
}

DEEP_DIVE = STYLE + """
Write the Friday deep dive on {sector}, the sector that triggered "Elsewhere" most often this week.
Cover it as fully as a core sector: what moved and why, the key news and earnings, the views
and debates (bull versus bear, named), and what to watch next week. About {words} words.
Return markdown with sub-headings ### What happened, ### Views and debates, ### What to watch.
"""
