"""Command line: python -m market_updater <command>

Commands
  collect         05:00 stage: fetch sources, transcribe, extract claims
  run             Build and publish today's briefing (weekdays only unless --force)
  dry-run         Build today's briefing without publishing; writes data/out/<date>.md
  health          Alert by Telegram if today's briefing is missing
  weekly          Earnings dates, fundamentals refresh, Substack discovery, settings backup
  discover        Substack discovery only
  export-settings Back up the current settings (YAML merged with Notion) to data/settings_backup/
  check-sources   Try every feed once and print which work
  sync-sources    Copy feed URLs from config/briefing.yaml into the Notion Sources table (once)
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone

from .util import log, now_london


def _check_sources() -> int:
    from .ingest.rss import fetch_feed
    from .publish import notion as N
    from .settings import load

    settings = load(N.connect(load()))
    since = datetime.now(timezone.utc) - timedelta(days=14)
    bad = 0
    for s in settings.sources:
        if not s.active:
            continue
        if not s.feed_url:
            print(f"  --  {s.name}: no feed URL")
            continue
        if not s.fetchable:
            continue
        try:
            items = fetch_feed(s, since, max_items=2, fetch_full=False)
            print(f"  ok  {s.name}: {len(items)} item(s) in 14 days")
        except Exception as e:
            bad += 1
            print(f"  XX  {s.name}: {e}")
    return 1 if bad else 0


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env reader so manual runs pick up keys; real environment variables win."""
    import os
    from pathlib import Path

    f = Path(os.environ.get("MU_ENV_FILE", path))
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    p = argparse.ArgumentParser(prog="market_updater")
    p.add_argument("command", choices=["collect", "run", "dry-run", "health", "weekly", "discover",
                                       "export-settings", "check-sources", "sync-sources"])
    p.add_argument("--force", action="store_true", help="Run even at weekends")
    p.add_argument("--no-collect", action="store_true", help="Skip fetching sources (reuse the database)")
    a = p.parse_args(argv)

    from . import run as R

    if a.command == "collect":
        if now_london().weekday() >= 5 and not a.force:
            print("Weekend: nothing to do (use --force).")
            return 0
        out = R.collect_stage()
        print(out)
        return 0
    if a.command in ("run", "dry-run"):
        if now_london().weekday() >= 5 and not a.force:
            print("Weekend: nothing to do (use --force).")
            return 0
        res = R.daily(dry_run=a.command == "dry-run", collect=not a.no_collect)
        print(f"{res.day}: {res.status}. {res.url or ''}")
        for pr in res.problems[:20]:
            print(f"  - {pr}")
        return 0 if res.status in ("Published", "Partial") or a.command == "dry-run" else 1
    if a.command == "health":
        out = R.health()
        print(out)
        return 0 if out in ("ok", "weekend") else 1
    if a.command == "weekly":
        print(R.weekly())
        return 0
    if a.command == "discover":
        from . import discovery as D
        from .db import DB
        from .llm import LLM
        from .publish import notion as N
        from .settings import load
        notion = N.connect(load())
        settings = load(notion)
        db = DB()
        print(D.run(settings, db, LLM(settings, db), notion))
        return 0
    if a.command == "export-settings":
        print(R.export_settings())
        return 0
    if a.command == "check-sources":
        return _check_sources()
    if a.command == "sync-sources":
        from .publish import notion as N
        from .publish.pages import sync_sources
        from .settings import load_yaml
        notion = N.connect(load_yaml())
        if notion is None:
            print("NOTION_TOKEN is not set.")
            return 1
        out = sync_sources(notion, load_yaml().sources)
        print(f"Filled {len(out['filled'])} feed URLs; added {len(out['added'])} sources.")
        return 0
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # last-resort alert
        log.exception("Fatal: %s", e)
        try:
            from .publish import telegram
            telegram.send(f"Market Updater crashed: {e}")
        finally:
            sys.exit(1)
