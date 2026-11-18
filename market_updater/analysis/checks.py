"""Safety checks on drafted text: every number and every citation must trace to a source.

- Citations are written as [c123] (claim ids) or [m] (market tables). Unknown ids are removed.
- Any number in a sentence must appear in a cited claim, its evidence quote, or the market
  tables. Sentences with unmatched numbers are dropped and logged.
"""
from __future__ import annotations

import re

NUM = re.compile(r"(?<![\w.])[-+−]?\$?£?€?\d[\d,]*(?:\.\d+)?\s?(?:%|bp|bps|bn|m|k|x|tn)?", re.I)
CITE = re.compile(r"\[c(\d+)\]")
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z*\[(\"'])")
# Numbers that are not data: years, dates, ordinals, counts of things in the briefing itself.
SAFE = re.compile(r"^(?:19|20)\d\d$|^\d$|^Q[1-4]$", re.I)


def _norm(n: str) -> str:
    n = n.strip().lower().replace("−", "-").replace(",", "").replace(" ", "")
    n = n.lstrip("+$£€")
    return n


NAMES_WITH_NUMBERS = re.compile(
    r"\b(?:FTSE\s(?:100|250|350)|S&P\s500|Nasdaq(?:\s|-)100|(?:Euro\s)?Stoxx\s(?:50|600)|"
    r"Nikkei\s225|CSI\s300|Russell\s(?:1000|2000)|DAX\s40|CAC\s40|Topix\s500|G7|G20|Fortune\s500)\b", re.I)
# Exchange tickers such as 7203.T, 1211.HK, EXV1.DE or BZ=F (case-sensitive so 12.34 is untouched).
TICKERS_WITH_NUMBERS = re.compile(r"\b(?:\d{3,6}|[A-Z][A-Z0-9]{1,5})\.(?:[A-Z]{1,2})\b|\b[A-Z]{2,3}=F\b|\^\w+")


def numbers_in(text: str) -> list[str]:
    text = TICKERS_WITH_NUMBERS.sub(" ", NAMES_WITH_NUMBERS.sub(" ", text))
    out = []
    for m in NUM.finditer(text):
        raw = m.group(0).strip()
        core = re.sub(r"[^\d.]", "", raw)
        if not core or SAFE.match(core) or SAFE.match(raw):
            continue
        out.append(raw)
    return out


def _variants(n: str) -> set[str]:
    v = _norm(n)
    base = re.sub(r"(%|bp|bps|bn|m|k|x|tn)$", "", v)
    out = {v, base}
    try:
        f = float(base)
        out |= {f"{f:.0f}", f"{f:.1f}", f"{f:.2f}", f"{abs(f):.1f}", f"{abs(f):.2f}", f"{abs(f):.0f}",
                f"{f:g}", f"{abs(f):g}"}
    except ValueError:
        pass
    return out


def _draft_forms(n: str) -> set[str]:
    """How a drafted number may be matched: exactly, never by rounding it further.

    A source's 4.12% supports a draft's 4.1%, but a draft's 12.34% is not supported by a source's 12.
    """
    v = _norm(n)
    base = re.sub(r"(%|bp|bps|bn|m|k|x|tn)$", "", v)
    out = {v, base}
    try:
        out.add(f"{float(base):g}")
    except ValueError:
        pass
    return out


def allowed_numbers(texts: list[str]) -> set[str]:
    allowed: set[str] = set()
    for t in texts:
        for n in numbers_in(t or ""):
            allowed |= _variants(n)
    return allowed


def check_section(markdown: str, claims: dict[int, dict], market_numbers: set[str],
                  extra_texts: list[str] | None = None) -> tuple[str, list[str]]:
    """Return (clean markdown, problems). Tables and headings pass through untouched."""
    problems: list[str] = []
    base_allowed = {_norm(x) for x in market_numbers}
    base_allowed |= allowed_numbers(list(market_numbers))
    base_allowed |= allowed_numbers(extra_texts or [])
    out_lines = []
    for line in markdown.splitlines():
        if line.lstrip().startswith(("|", "#")) or not line.strip():
            out_lines.append(line)
            continue
        kept = []
        for sent in SENT_SPLIT.split(line):
            ids = [int(x) for x in CITE.findall(sent)]
            unknown = [i for i in ids if i not in claims]
            for i in unknown:
                sent = re.sub(rf"\s*\[c{i}\]", "", sent)
                problems.append(f"removed unknown citation c{i}")
            texts = []
            for i in ids:
                if i in claims:
                    c = claims[i]
                    texts += [c.get("claim", ""), c.get("evidence", "") or c.get("evidence_quote", ""),
                              " ".join(c.get("numbers") or [])]
            allowed = base_allowed | allowed_numbers(texts)
            bad = [n for n in numbers_in(CITE.sub("", sent)) if not (_draft_forms(n) & allowed)]
            if bad:
                problems.append(f"dropped sentence with unsourced number(s) {bad}: {sent[:120]}")
                continue
            kept.append(sent)
        if kept:
            out_lines.append(" ".join(kept))
    return "\n".join(out_lines), problems


def link_citations(markdown: str, claims: dict[int, dict]) -> str:
    """Turn [c12] into a numbered source link, e.g. [[1]](url). Returns new markdown."""
    def repl(m):
        i = int(m.group(1))
        c = claims.get(i)
        if not c or not c.get("url") or str(c.get("url", "")).startswith("email:"):
            return ""
        return f"[↗]({c['url']})"
    return CITE.sub(repl, markdown)
