"""Earnings reviews (Claude Sonnet): reaction, numbers, what people said, what the call said."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..analysis.checks import CITE, check_section
from ..llm import LLMUnavailable
from ..market.earnings import Report
from ..market.snapshot import Snapshot
from ..settings import Settings
from ..util import log, truncate
from . import prompts as P
from .sections import claim_line


@dataclass
class Review:
    ticker: str
    verdict: str
    reaction_fit: str
    markdown: str
    cited: set[int] = field(default_factory=set)
    problems: list[str] = field(default_factory=list)


def _fmt(v, suffix=""):
    return "n/a" if v is None else f"{v:+.2f}{suffix}" if suffix == "%" else f"{v}{suffix}"


def review(llm, settings: Settings, rep: Report, claims: list[dict], snap: Snapshot) -> Review | None:
    people = settings.people
    mine = [c for c in claims if rep.ticker in (c.get("tickers") or [])][:25]
    stats = (f"Ticker {rep.ticker}; report date {rep.date}\n"
             f"Move on the day: {_fmt(rep.move_pct, '%')}; sector proxy: {_fmt(rep.sector_move_pct, '%')}; "
             f"excess: {_fmt(rep.excess_move, '%')}; usual absolute earnings-day move (last 8): "
             f"{'n/a' if rep.avg_abs_move_pct is None else f'{rep.avg_abs_move_pct:.2f}%'}\n"
             f"EPS actual {rep.eps_actual} vs estimate {rep.eps_estimate}; surprise "
             f"{'n/a' if rep.eps_surprise_pct is None else f'{rep.eps_surprise_pct:+.1f}%'}; "
             f"revenue actual {rep.rev_actual} vs estimate {rep.rev_estimate}\n"
             + ("\n".join(rep.notes)))
    transcript = ""
    if rep.transcript:
        transcript = f"\n\nTranscript [t] ({rep.transcript_source}):\n{truncate(rep.transcript, 60_000)}"
        if rep.prev_transcript:
            transcript += f"\n\nPrevious call transcript:\n{truncate(rep.prev_transcript, 35_000)}"
    user = (f"{stats}\n\nClaims:\n" + "\n".join(claim_line(c, people) for c in mine) + transcript)
    try:
        res = llm.json(tier="earnings_review", purpose=f"earnings_{rep.ticker}", system=P.EARNINGS_SYSTEM,
                       user=user, schema=P.EARNINGS_SCHEMA, max_tokens=2500)
    except LLMUnavailable as e:
        log.warning("Earnings review for %s skipped: %s", rep.ticker, e)
        return None
    by_id = {c["id"]: c for c in mine}
    extra = [stats, rep.transcript or "", rep.prev_transcript or ""]
    verdict, p1 = check_section(res.get("verdict", ""), by_id, snap.numbers, extra)
    body, p2 = check_section(res.get("review", "").replace("[t]", ""), by_id, snap.numbers, extra)
    rep.reaction_fit = res.get("reaction_fit")    # used by the trade-idea step
    return Review(rep.ticker, verdict, res.get("reaction_fit", "about right"), body,
                  {int(x) for x in CITE.findall(verdict + body)}, p1 + p2)


def pick(reports: list[Report], n: int) -> list[Report]:
    """Biggest surprises first: the larger of excess move vs usual and EPS surprise."""
    def score(r: Report):
        usual = r.avg_abs_move_pct or 3.0
        move = abs(r.excess_move or 0) / usual
        eps = abs(r.eps_surprise_pct or 0) / 10
        return max(move, eps)
    return sorted(reports, key=score, reverse=True)[:n]
