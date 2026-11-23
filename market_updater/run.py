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
from .util import LONDON, is_friday, log, now_london, word_set


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
    stats["summaries"] = _step("episode summaries", problems, X.summarise_long_items, db, llm, default=0)
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

    # 5. Trade ideas: pick up what the reader ticked in Notion, mark open ideas, pitch new ones
    from .publish import pages as PG
    if notion is not None and not dry_run:
        _step("sync following", problems, PG.sync_following, notion, db)
    idea_updates = _step("idea updates", problems, I.update_open, db, prices, settings, day, default=[]) or []
    from .analysis import memory as MEM
    _step("record calls", problems, MEM.record_calls, settings, db, claims, prices, day, default=0)
    _step("judge calls", problems, MEM.score_calls, db, prices, default=0)
    ideas = _step("trade ideas", problems, I.generate, settings, db, llm, prices, states, claims, reports, snap,
                  day, default=[]) or []
    checks = _step("thesis check", problems, I.thesis_check, llm, db, settings, claims, snap, idea_updates, day,
                   default=[]) or []

    # 6. Writing
    story = _step("top line", problems, S.market_story, llm, settings, snap, claims)
    core = [_step(f"sector {s.key}", problems, S.sector_section, llm, settings, s, snap, claims,
                  _words(settings, "core_sector", 70)) for s in settings.core_sectors]
    els = _step("elsewhere", problems, S.elsewhere_section, llm, settings, snap, claims)
    cons = _step("consensus check", problems, S.consensus_section, llm, settings, states, claims_by_id, snap)
    deep = None
    if day.weekday() in (1, 2, 3, 4):       # Tuesday to Friday: yesterday's busiest Elsewhere sector
        deep = _step("deep dive", problems, _deep_dive, settings, db, llm, snap, day, now)
    for part in [story, *core, els, cons, deep, *ideas, *reviews]:
        if part is not None and getattr(part, "problems", None):
            title = getattr(part, "title", None) or getattr(getattr(part, "sector", None), "name", None) or "part"
            problems += [f"{title}: {p}" for p in part.problems[:10]]

    # 7. Reading pages and charts (need Notion), then assemble
    names = F.company_names(db, sorted({r["ticker"] for rows in snap.sector_rows.values() for r in rows}))
    if notion is not None and not dry_run:
        for out in core:
            if out:
                out.reading_url = _step(f"reading {out.sector.key}", problems, PG.publish_reading, notion, day,
                                        out, S.reading_markdown(out, snap, names), claims_by_id)
        for idea in ideas:
            up = notion.upload_file(idea.chart, f"spread-{idea.spec['long']}-{idea.spec['short']}.png") if idea.chart else None
            idea.markdown = idea.markdown.replace(
                "{{chart}}", f"![The hedged spread over the last year, with entry, target and stop](upload:{up})" if up else "")
    for idea in ideas:
        idea.markdown = idea.markdown.replace("{{chart}}", "")
    dash = (settings.get("notion.pages", {}) or {}).get("dashboard")
    md, cited = assemble(settings, day, snap, story, ideas, idea_updates, core, reviews, els, cons, deep,
                         db, claims_by_id, pending, dash, checks)
    failed_core = sum(1 for s in core if not s or not s.blurb)
    status = "Published" if failed_core == 0 and story else "Partial"

    # 8. Publish
    url = None
    out_dir = DATA_DIR / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{day.isoformat()}.md").write_text(md)
    if not dry_run and notion is not None:
        themes = [b.split(":**")[0].strip("- *") for b in (story.bullets if story else [])][:5]
        moved = sorted({SECTOR_LABELS.get(t.sector, settings.sector(t.sector).name if settings.sector(t.sector) else t.sector)
                        for t in snap.triggers})
        res = _step("publish", problems, PG.publish_briefing, notion, day,
                    f"Market briefing {day.strftime('%a %d %b %Y')}", md, status=status, themes=themes,
                    sectors_moved=moved, quiet=not snap.triggers, deep_dive=deep.title if deep else "")
        url, page_id = res if res else (None, None)
        _step("save ideas", problems, I.save, db, ideas, day, url)
        _step("push ideas", problems, PG.push_ideas, notion, db,
              {u["id"]: f"{u['day']} of {u['max_days']}" for u in idea_updates},
              {(i.spec["long"], i.spec["short"]): i.chart for i in ideas if i.chart})
        _step("push views", problems, PG.push_views, notion, db)
        _step("propose debates", problems, PG.propose_debates, notion,
              V.proposed_debates(db, now - timedelta(days=7)), set(settings.debates))
        if page_id:
            _step("home page", problems, PG.update_home, notion, settings, day, page_id,
                  story.headline if story else "")
            for out in core:
                if out and out.reading_url:
                    _step("reading link", problems, PG.link_reading, notion, out.reading_url, url)
        if url is None:
            status = "Failed"
    _record_stories(db, day, claims, cited)
    db.finish_run(run_id, status, json.dumps({"problems": problems[:50], "stats": stats}, default=str))
    if status != "Published" and not dry_run:
        telegram.send(f"Market Updater {day}: {status}. {len(problems)} problem(s). First: "
                      f"{problems[0] if problems else 'n/a'}")
    return RunResult(day, md, status, url, problems, stats)


def published_today(db: DB | None = None) -> bool:
    """True if a daily run already finished as Published or Partial today (London date)."""
    db = db or DB()
    today = now_london().date()
    start = datetime(today.year, today.month, today.day, tzinfo=LONDON).astimezone(timezone.utc)
    row = db.one("SELECT COUNT(*) n FROM runs WHERE kind='daily' AND status IN ('Published','Partial') "
                 "AND started>=?", (start.strftime("%Y-%m-%dT%H:%M:%S"),))
    return bool(row and row["n"])


def _deep_dive(settings: Settings, db: DB, llm, snap, day: date, now: datetime):
    """Tuesday to Friday: the Elsewhere sector that triggered most on the previous trading day."""
    prev = day - timedelta(days=3 if day.weekday() == 0 else 1)
    ranked = db.q("SELECT sector, COUNT(*) n, SUM(magnitude) m, GROUP_CONCAT(reason, '; ') why FROM elsewhere_log "
                  "WHERE date=? GROUP BY sector ORDER BY n DESC, m DESC LIMIT 3", (prev.isoformat(),))
    if not ranked:
        log.info("No Elsewhere moves on %s, so no deep dive today", prev)
        return None
    recent = [X.claim_dict(c) for c in db.claims_since(now - timedelta(days=4))]
    for top in ranked:
        sec = settings.sector(top["sector"])
        if not sec or sec.core:
            continue
        out = S.deep_dive(llm, settings, top["sector"], f"{prev.strftime('%A')}: {top['why']}", snap, recent,
                          _words(settings, "deep_dive", 600))
        if out and out.markdown:
            return out
    return None


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


MOOD = {"risk_off": ("🔴", "red"), "risk_on": ("🟢", "green"), "mixed": ("⚪", "gray")}
CONSENSUS_PURPOSE = ("*Where the crowd is leaning and whether anyone credible is betting against it: "
                     "crowded views are where the best contrarian trades come from.*")


def _table(head: list[str], rows: list[list[str]]) -> str:
    clean = [[str(c).replace("|", "/").replace("\n", " ") for c in r] for r in rows]
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
                     + ["| " + " | ".join(r) + " |" for r in clean])


def assemble(settings, day, snap, story, ideas, idea_updates, core, reviews, els, cons, deep,
             db, claims_by_id, pending: list[str] | None = None,
             dashboard_id: str | None = None, checks: list[dict] | None = None) -> tuple[str, set[int]]:
    from .analysis.checks import link_citations, tidy

    parts: list[str] = []
    cited: set[int] = set()

    def add(title: str, body: str, ids: set[int] | None = None):
        if body and body.strip():
            parts.append(f"## {title}\n{body.strip()}")
            cited.update(ids or set())

    # Top line
    if story:
        icon, color = MOOD.get(story.mood, MOOD["mixed"])
        moves = S.headline_moves(snap)
        body = [f"::: callout {icon} {color}", f"**{story.headline}** {moves}".strip(), ":::"] + story.bullets
        add("☀️ Top line", "\n".join(body), story.cited)
    elif snap.key_rows:
        add("☀️ Top line", f"::: callout ⚪ gray\n{S.headline_moves(snap)}\n:::")

    # The numbers that matter
    if snap.key_rows:
        so = story.so_what if story else {}
        rows = [[r["name"], r["level"], r["move"], so.get(r["name"], "")] for r in snap.key_rows]
        add("📊 The numbers that matter", _table(["What", "Level", "Move", "So what"], rows))

    # Trade ideas
    idea_md = "\n".join(i.markdown for i in ideas)
    if len(ideas) < 3:
        idea_md += (f"\n*Only {len(ideas)} idea{'s' if len(ideas) != 1 else ''} passed today's review. Every pitch "
                    "needs a well-correlated pair, evidence on both legs and a reviewer score of at least 7/10, "
                    "and weaker ideas are not pitched.*")
    add("🎯 Trade ideas", idea_md, {c for i in ideas for c in i.cited})
    add("📋 Open ideas: is the thesis playing out?", I.book_markdown(checks or [], idea_updates, dashboard_id))

    # Core sectors
    blocks = []
    for out in core:
        if not out:
            continue
        icon = S.ICONS.get(out.sector.key, "📌")
        more = f" Read more → [{out.sector.name} reading](notion:{_page_id(out.reading_url)})" if out.reading_url else ""
        blocks += [f"::: callout {icon} blue", f"**{out.sector.name}** — {out.blurb or 'A quiet day.'}{more}", ":::"]
        cited.update(out.cited)
    add("🏭 Your sectors", "\n".join(blocks))

    # Earnings
    if reviews or pending:
        body = "\n".join(f"**{r.ticker}:** {tidy(r.verdict)}\n::: toggle Full review: {r.ticker}\n{tidy(r.markdown)}\n:::"
                         for r in reviews)
        if pending:
            body += f"\n*Reported, reaction still to come (reviewed tomorrow): {', '.join(pending)}.*"
        add("📅 Earnings reviews", body, {c for r in reviews for c in r.cited})

    # Elsewhere
    if els and els.rows:
        add("🌍 Elsewhere", _table(["Sector", "Move", "What's behind it"],
                                   [[r["sector"], r["move"], r["why"]] for r in els.rows]), els.cited)

    # Consensus check
    if cons and cons.rows:
        add("🧭 Consensus check", CONSENSUS_PURPOSE + "\n" + _table(
            ["Debate", "Crowd", "Change", "Worth noting"],
            [[r["debate"], r["crowd"], r["change"], r["note"]] for r in cons.rows]), cons.cited)

    # Deep dive
    if deep and deep.markdown:
        add(f"🔍 {deep.title}", deep.markdown, deep.cited)

    # Friday scorecard
    if is_friday(day):
        from .analysis import memory as MEM
        add("🏁 Trade idea scorecard", I.scorecard(db))
        add("🗣 Who has been right", "*Every bullish or bearish call your sources make on a stock we track is "
            "judged after 20 trading days against its sector ETF. The trade ideas lean on the voices with the "
            "best record.*\n" + MEM.scorecard_markdown(db))

    # Footer: links to the reading pages
    links = [f"{S.ICONS.get(o.sector.key, '')} [{o.sector.name}](notion:{_page_id(o.reading_url)})"
             for o in core if o and o.reading_url]
    if links:
        parts.append("---\n**Read more by sector:** " + " · ".join(links))
    md = link_citations("\n\n".join(parts), claims_by_id)
    return md, cited


def _page_id(url: str | None) -> str:
    """The 32-character id at the end of a Notion URL."""
    import re as _re
    m = _re.search(r"([0-9a-f]{32})(?:\?|$)", (url or "").replace("-", ""))
    return m.group(1) if m else (url or "")


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
