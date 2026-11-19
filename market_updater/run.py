"""The daily run, the weekly maintenance run, the health check and helpers.

Every step is wrapped so one failure degrades the page instead of killing the run.
"""
from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .analysis import extract as X
from .analysis import views as V
from .compose import earnings_review as ER
from .compose import ideas as I
from .compose import sections as S
from .db import DB
from .ingest.collect import collect_all, lookback
from .llm import LLM
from .market import earnings as E
from .market import fundamentals as F
from .market import macro as M
from .market import snapshot as SN
from .market.prices import Prices
from .publish import notion as N
from .publish import telegram
from .settings import DATA_DIR, SECTOR_LABELS, Settings, load
from .util import is_friday, log, now_london, word_set


@dataclass
class RunResult:
    day: date
    markdown: str
    status: str
    url: str | None = None
    problems: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def _step(name: str, problems: list[str], fn, *a, default=None, **kw):
    t0 = time.monotonic()
    try:
        out = fn(*a, **kw)
        if time.monotonic() - t0 > 20:
            log.info("Step %s took %.0fs", name, time.monotonic() - t0)
        return out
    except Exception as e:
        log.error("Step %s failed: %s\n%s", name, e, traceback.format_exc(limit=3))
        problems.append(f"{name}: {e}")
        return default


def _words(settings: Settings, key: str, default: int) -> int:
    return int((settings.get("budget.words_per_section", {}) or {}).get(key, default))


def _market_tickers(settings: Settings) -> list[str]:
    mk = settings.get("markets", {}) or {}
    out = [m["ticker"] for g in ("indices", "commodities") for m in mk.get(g, []) if m.get("ticker")]
    for s in settings.sectors:
        if s.coverage != "off":
            out += s.proxies + s.watchlist
    return sorted(set(out))


def daily(*, dry_run: bool = False, settings: Settings | None = None, db: DB | None = None,
          llm=None, prices: Prices | None = None, notion=None, now: datetime | None = None,
          collect: bool = True) -> RunResult:
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(now_london().tzinfo).date()
    problems: list[str] = []
    if notion is None and not dry_run:
        base = settings or load()
        notion = _step("notion connect", problems, N.connect, base)
    settings = settings or load(notion)
    db = db or DB()
    llm = llm or LLM(settings, db)
    prices = prices or Prices(db, disagreement_pct=float(settings.get("thresholds.price_source_disagreement_pct", 0.5)))
    run_id = db.start_run("daily")
    since = lookback(now)
    stats: dict = {}

    # 1. Prices first (needed for headlines' company names and everything after)
    _step("prices", problems, prices.load, _market_tickers(settings))

    # 2. Collect and extract
    if collect:
        names = F.company_names(db, settings.all_watchlist)
        stats["collect"] = _step("collect", problems, collect_all, settings, db, notion, now, names, default={})
    stats["extract"] = _step("extract", problems, X.extract_pending, settings, db, llm, default={})
    new_views = _step("views", problems, V.record_views, settings, db, since, default=[]) or []

    # 3. Macro, earnings, snapshot
    macro = _step("macro", problems, M.collect, settings, default=[]) or []
    reports = _step("earnings calendar", problems, E.reporters, db, settings.all_watchlist, day, default=[]) or []
    for r in reports:
        sec = settings.sector(settings.sector_of_ticker(r.ticker) or "")
        _step(f"reaction {r.ticker}", problems, E.add_reaction, r, prices.frame,
              sec.proxies[0] if sec and sec.proxies else None, db)
    pending = sorted(r.ticker for r in reports if r.pending)
    reports = [r for r in reports if E.reviewable(r, prices.frame)]
    surprises = [(r.ticker, r.eps_surprise_pct) for r in reports if r.eps_surprise_pct is not None]
    asof = now.astimezone(now_london().tzinfo).strftime("%H:%M London time")
    t0 = time.monotonic()
    snap = SN.build(settings, prices, macro, db, day, asof, surprises)
    log.info("Snapshot built in %.0fs", time.monotonic() - t0)

    claims = [X.claim_dict(c) for c in db.claims_since(since)]
    claims_by_id = {c["id"]: c for c in claims}
    states = _step("debates", problems, V.debate_states, settings, db, day, default=[]) or []

    # 4. Earnings reviews
    n_reviews = int(settings.get("earnings.full_reviews_per_day", 5))
    reviews = []
    for rep in ER.pick(reports, n_reviews):
        _step(f"transcripts {rep.ticker}", problems, E.add_transcripts, rep, db, day)
        rv = _step(f"review {rep.ticker}", problems, ER.review, llm, settings, rep, claims, snap)
        if rv:
            reviews.append(rv)

    # 5. Trade ideas
    idea_updates = _step("idea updates", problems, I.update_open, db, prices, settings, day, default=[]) or []
    ideas = _step("trade ideas", problems, I.generate, settings, db, llm, prices, states, claims, reports, snap,
                  default=[]) or []
    if not dry_run:
        _step("save ideas", problems, I.save, db, ideas, day)

    # 6. Sections
    secs: list[S.Section] = []
    ov = _step("overview", problems, S.overview, llm, settings, snap, claims, _words(settings, "market_overview", 500))
    core = [_step(f"sector {s.key}", problems, S.sector_section, llm, settings, s, snap, claims,
                  _words(settings, "core_sector", 450)) for s in settings.core_sectors]
    els = _step("elsewhere", problems, S.elsewhere_section, llm, settings, snap, claims, _words(settings, "elsewhere", 200))
    div = _step("diverge", problems, S.diverge_section, llm, settings, states, claims_by_id, snap,
                _words(settings, "views_diverge", 450))
    deep = None
    if is_friday(day):
        ranked = db.q("SELECT sector, COUNT(*) n, SUM(magnitude) m FROM elsewhere_log WHERE date>=? "
                      "GROUP BY sector ORDER BY n DESC, m DESC LIMIT 3", ((day - timedelta(days=6)).isoformat(),))
        week = [X.claim_dict(c) for c in db.claims_since(now - timedelta(days=7))] if ranked else []
        for top in ranked:   # the most-triggered sector with enough material to write about
            deep = _step("deep dive", problems, S.deep_dive, llm, settings, top["sector"], snap, week,
                         _words(settings, "friday_deep_dive", 700))
            if deep:
                break
    for s in [ov, *core, els, div, deep]:
        if s:
            secs.append(s)
            problems += [f"{s.title}: {p}" for p in s.problems]
    top_line = _step("top line", problems, S.topline, llm, secs, claims_by_id, snap)

    # 7. Assemble
    md, cited = assemble(settings, day, snap, top_line, ov, ideas, idea_updates, core, reviews, els, div, deep,
                         db, claims_by_id, now, pending)
    failed_core = sum(1 for s in core if not s or "Not available today" in s.markdown)
    status = "Published" if failed_core == 0 and top_line else "Partial"

    # 8. Publish
    url = None
    out_dir = DATA_DIR / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{day.isoformat()}.md").write_text(md)
    if not dry_run and notion is not None:
        themes = [d.question for d in states[:3]]
        moved = sorted({SECTOR_LABELS.get(t.sector, settings.sector(t.sector).name if settings.sector(t.sector) else t.sector)
                        for t in snap.triggers})
        from .publish import pages as PG
        url = _step("publish", problems, PG.publish_briefing, notion, day,
                    f"Market briefing {day.strftime('%a %d %b %Y')}", md, status=status, themes=themes,
                    sectors_moved=moved, quiet=not snap.triggers, deep_dive=deep.title if deep else "")
        _step("push views", problems, PG.push_views, notion, db)
        _step("push ideas", problems, PG.push_ideas, notion, db)
        _step("propose debates", problems, PG.propose_debates, notion,
              V.proposed_debates(db, now - timedelta(days=7)), set(settings.debates))
        if url is None:
            status = "Failed"
    _record_stories(db, day, claims, cited)
    db.finish_run(run_id, status, json.dumps({"problems": problems[:50], "stats": stats}, default=str))
    if status != "Published" and not dry_run:
        telegram.send(f"Market Updater {day}: {status}. {len(problems)} problem(s). First: "
                      f"{problems[0] if problems else 'n/a'}")
    return RunResult(day, md, status, url, problems, stats)


def collect_stage(now: datetime | None = None) -> dict:
    """05:00 stage: fetch sources, transcribe, extract claims and record views."""
    now = now or datetime.now(timezone.utc)
    problems: list[str] = []
    notion = _step("notion connect", problems, N.connect, load())
    settings = load(notion)
    db = DB()
    llm = LLM(settings, db)
    run_id = db.start_run("collect")
    names = F.company_names(db, settings.all_watchlist)
    stats = {"collect": _step("collect", problems, collect_all, settings, db, notion, now, names, default={}),
             "extract": _step("extract", problems, X.extract_pending, settings, db, llm, default={})}
    _step("views", problems, V.record_views, settings, db, lookback(now))
    db.finish_run(run_id, "Done" if not problems else "Partial",
                  json.dumps({"problems": problems, "stats": stats}, default=str))
    return {"problems": problems, **stats}


def assemble(settings, day, snap, top_line, ov, ideas, idea_updates, core, reviews, els, div, deep,
             db, claims_by_id, now: datetime | None = None,
             pending: list[str] | None = None) -> tuple[str, set[int]]:
    from .analysis.checks import link_citations

    parts: list[str] = []
    cited: set[int] = set()

    def add(title: str, body: str, ids: set[int] | None = None):
        if body and body.strip():
            parts.append(f"## {title}\n{body.strip()}")
            cited.update(ids or set())

    prepared = (now.astimezone(now_london().tzinfo) if now else now_london()).strftime('%H:%M')
    parts.append(f"*Prepared {prepared} London time. Ideas are for research, "
                 f"not investment advice. Numbers come from market data feeds; every view links to its source.*")
    if top_line:
        add("Top line", top_line.markdown, top_line.cited)
    ov_body = f"*Markets as of {snap.asof_label}; Asia still trading.*\n{snap.overview_md}\n\n::: toggle Rates, credit and macro data\n{snap.macro_md}\n:::"
    if ov:
        ov_body += "\n" + ov.markdown
    add("Market overview", ov_body, ov.cited if ov else set())
    idea_md = "\n\n".join(i.markdown for i in ideas)
    if idea_updates:
        idea_md += "\n\n**Open ideas**\n" + "\n".join(idea_updates)
    add("Trade ideas", idea_md or "- No idea passed the checks today.", {c for i in ideas for c in i.cited})
    for sec, s in zip(settings.core_sectors, core):
        body = f"::: toggle Moves\n{snap.sector_tables.get(sec.key, '')}\n:::\n" + (s.markdown if s else "- Not available today.")
        add(sec.name, body, s.cited if s else set())
    if reviews or pending:
        body = "\n".join(f"**{r.ticker}:** {r.verdict}\n::: toggle Full review: {r.ticker}\n{r.markdown}\n:::"
                         for r in reviews)
        if pending:
            body += (f"\n*Reported, reaction still to come (reviewed tomorrow): {', '.join(pending)}.*")
        add("Earnings reviews", body, {c for r in reviews for c in r.cited})
    if els:
        add("Elsewhere", els.markdown, els.cited)
    if div:
        add("Where views diverge", div.markdown, div.cited)
    if deep:
        add(deep.title, deep.markdown, deep.cited)
    if is_friday(day):
        add("Trade idea scorecard", I.scorecard(db, (day - timedelta(days=6)).isoformat()))
        add("Source health", source_health(settings, db))
    # Sources list
    src_lines = []
    for cid in sorted(cited):
        c = claims_by_id.get(cid)
        if c and str(c.get("url", "")).startswith("http"):
            src_lines.append(f"- [{c.get('item_title') or c.get('source_name')}]({c['url']}) "
                             f"({c.get('source_name')})")
    seen, uniq = set(), []
    for l in src_lines:
        if l not in seen:
            seen.add(l)
            uniq.append(l)
    if uniq:
        add("Sources", "::: toggle All sources used\n" + "\n".join(uniq) + "\n:::")
    md = link_citations("\n\n".join(parts), claims_by_id)
    return md, cited


def source_health(settings: Settings, db: DB) -> str:
    cutoff = (datetime.utcnow() - timedelta(days=3)).isoformat(timespec="seconds")
    lines = []
    for s in settings.sources:
        if not s.fetchable:
            continue
        r = db.one("SELECT * FROM source_health WHERE source=?", (s.name,))
        if not r or not r["last_success"] or r["last_success"] < cutoff:
            err = (r["last_error"] if r else None) or "no successful fetch yet"
            lines.append(f"- {s.name}: silent for 3+ days ({err[:120]})")
    missing = [s.name for s in settings.sources if s.active and not s.feed_url
               and s.type not in ("press_mentions", "email", "api", "podcast")]
    if missing:
        lines.append(f"- No feed URL yet: {', '.join(missing[:20])}")
    spend = db.month_spend("anthropic")
    lines.append(f"- Claude spend this month: ${spend:.2f} of the $100 credit.")
    return "\n".join(lines) or "- All sources healthy."


def _record_stories(db: DB, day: date, claims: list[dict], cited: set[int]) -> None:
    titles = {c["item_title"] for c in claims
              if c["id"] in cited and c.get("item_title") and c.get("kind") in ("fact", "data")}
    with db.tx() as conn:
        conn.executemany("INSERT INTO stories(date,title,words) VALUES (?,?,?)",
                         [(day.isoformat(), t, json.dumps(sorted(word_set(t)))) for t in titles])


def health() -> str:
    """Run at 06:55: alert if today's page is missing."""
    settings = load()
    day = now_london().date()
    if day.weekday() >= 5:
        return "weekend"
    db = DB()
    row = db.one("SELECT status FROM runs WHERE kind='daily' AND started>=? ORDER BY id DESC LIMIT 1",
                 ((datetime.utcnow() - timedelta(hours=4)).isoformat(timespec="seconds"),))
    ok = bool(row and row["status"] in ("Published", "Partial"))
    notion = N.connect(settings)
    if notion is not None:
        try:
            found = notion.query("briefings", filter={"property": "Date", "date": {"equals": day.isoformat()}})
            ok = ok or bool(found)
        except Exception as e:
            log.warning("Health check could not read Notion: %s", e)
    if not ok:
        telegram.send(f"Market Updater: no briefing for {day} yet. Last run status: "
                      f"{row['status'] if row else 'none'}. Check `journalctl -u market-updater`.")
        return "missing"
    return "ok"


def weekly() -> dict:
    """Sunday maintenance: earnings dates, fundamentals cache, discovery, settings backup."""
    problems: list[str] = []
    notion = _step("notion", problems, N.connect, load())
    settings = load(notion)
    db = DB()
    llm = LLM(settings, db)
    _step("earnings dates", problems, E.refresh_yahoo_dates, db, settings.all_watchlist)
    for t in settings.all_watchlist:
        _step(f"fundamentals {t}", problems, F.get, db, t)
    from . import discovery as D
    stats = _step("discovery", problems, D.run, settings, db, llm, notion, default={}) or {}
    if notion is not None:
        from .publish import pages as PG
        _step("discovery decisions", problems, PG.sync_discovery_decisions, notion, db)
    _step("settings backup", problems, export_settings, settings)
    return {"problems": problems, **stats}


def export_settings(settings: Settings | None = None) -> Path:
    settings = settings or load(N.connect(load()))
    out = DATA_DIR / "settings_backup"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{now_london().date().isoformat()}.json"
    path.write_text(settings.to_json())
    return path
