"""SQLite storage: items, claims, views, stories, ideas, caches and run logs."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .settings import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    url TEXT UNIQUE,
    fingerprint TEXT,
    source_name TEXT,
    source_type TEXT,
    kind TEXT,
    title TEXT,
    published TEXT,
    fetched TEXT,
    text TEXT,
    is_subscriber INTEGER DEFAULT 0,
    sectors TEXT DEFAULT '[]',
    extracted INTEGER DEFAULT 0,
    repeat_of TEXT
);
CREATE INDEX IF NOT EXISTS ix_items_fp ON items(fingerprint);
CREATE INDEX IF NOT EXISTS ix_items_fetched ON items(fetched);

CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY,
    item_id INTEGER REFERENCES items(id),
    kind TEXT, speaker TEXT, via TEXT, sector TEXT, tickers TEXT, debate TEXT,
    stance TEXT, confidence TEXT, claim TEXT, evidence TEXT, numbers TEXT,
    created TEXT
);
CREATE INDEX IF NOT EXISTS ix_claims_created ON claims(created);

CREATE TABLE IF NOT EXISTS views (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER REFERENCES claims(id),
    speaker TEXT, debate TEXT, stance TEXT, date TEXT, url TEXT,
    priority INTEGER DEFAULT 0, contrarian INTEGER DEFAULT 0,
    changed INTEGER DEFAULT 0, prev_stance TEXT, prev_date TEXT, prev_url TEXT,
    pushed INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_views_sd ON views(speaker, debate, date);

CREATE TABLE IF NOT EXISTS stories (
    id INTEGER PRIMARY KEY, date TEXT, title TEXT, words TEXT
);

CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY, notion_page_id TEXT, created TEXT,
    long TEXT, short TEXT, sector TEXT, origin TEXT,
    hedge_ratio REAL, corr REAL,
    entry_long REAL, entry_short REAL, entry_spread REAL, spread_std REAL, stop_spread REAL,
    status TEXT DEFAULT 'Open', closed TEXT, close_reason TEXT,
    thesis TEXT, wrong TEXT, against_consensus INTEGER, confidence TEXT,
    source_names TEXT, pnl REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT, date TEXT, close REAL, PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS fundamentals (
    ticker TEXT PRIMARY KEY, fetched TEXT, data TEXT
);

CREATE TABLE IF NOT EXISTS earnings_dates (
    ticker TEXT, date TEXT, hour TEXT, PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS elsewhere_log (
    date TEXT, sector TEXT, reason TEXT, magnitude REAL
);

CREATE TABLE IF NOT EXISTS llm_usage (
    ts TEXT, provider TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER,
    cost_usd REAL, purpose TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, kind TEXT, started TEXT, finished TEXT, status TEXT, notes TEXT
);

CREATE TABLE IF NOT EXISTS source_health (
    source TEXT PRIMARY KEY, last_success TEXT, last_error TEXT, last_error_at TEXT
);

CREATE TABLE IF NOT EXISTS discovery (
    pub_url TEXT PRIMARY KEY, name TEXT, first_seen TEXT, score REAL,
    found_via TEXT, example_post TEXT, why TEXT, sectors TEXT, decision TEXT DEFAULT 'Undecided',
    pushed INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS feed_lookup (
    name TEXT PRIMARY KEY, url TEXT, checked TEXT
);

CREATE TABLE IF NOT EXISTS transcribed (
    audio_url TEXT PRIMARY KEY, item_id INTEGER, done TEXT
);
"""


class DB:
    def __init__(self, path: Path | str | None = None):
        path = Path(path) if path else DATA_DIR / "market_updater.db"
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    # Columns added after the first release; ALTER TABLE is a no-op if they already exist.
    MIGRATIONS = {
        "ideas": [("headline", "TEXT"), ("target_spread", "REAL"), ("target_return", "REAL"),
                  ("stop_return", "REAL"), ("horizon_end", "TEXT"), ("following", "INTEGER DEFAULT 0"),
                  ("best", "REAL"), ("worst", "REAL"), ("outcome", "TEXT DEFAULT 'Open'"),
                  ("briefing_url", "TEXT"), ("spec", "TEXT")],
    }

    def _migrate(self) -> None:
        for table, cols in self.MIGRATIONS.items():
            have = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def q(self, sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple | list = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    def ex(self, sql: str, args: tuple | list = ()) -> int:
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur.lastrowid or 0

    # Items ------------------------------------------------------------------
    def seen_url(self, url: str) -> bool:
        return self.one("SELECT 1 FROM items WHERE url=?", (url,)) is not None

    def seen_fingerprint(self, fp: str) -> bool:
        return self.one("SELECT 1 FROM items WHERE fingerprint=?", (fp,)) is not None

    def add_item(self, *, url: str, fingerprint: str, source_name: str, source_type: str, kind: str,
                 title: str, published: str, text: str, is_subscriber: bool = False,
                 sectors: list[str] | None = None, repeat_of: str | None = None) -> int | None:
        try:
            return self.ex(
                "INSERT INTO items(url,fingerprint,source_name,source_type,kind,title,published,fetched,"
                "text,is_subscriber,sectors,repeat_of) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (url, fingerprint, source_name, source_type, kind, title, published,
                 datetime.utcnow().isoformat(timespec="seconds"), text, int(is_subscriber),
                 json.dumps(sectors or []), repeat_of))
        except sqlite3.IntegrityError:
            return None

    def pending_items(self, since_hours: int = 30) -> list[sqlite3.Row]:
        cutoff = (datetime.utcnow() - timedelta(hours=since_hours)).isoformat(timespec="seconds")
        return self.q("SELECT * FROM items WHERE extracted=0 AND fetched>=? ORDER BY id", (cutoff,))

    def items_since(self, since: datetime) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM items WHERE fetched>=? ORDER BY id",
                      (since.isoformat(timespec="seconds"),))

    # Claims -----------------------------------------------------------------
    def add_claim(self, item_id: int, c: dict[str, Any]) -> int:
        return self.ex(
            "INSERT INTO claims(item_id,kind,speaker,via,sector,tickers,debate,stance,confidence,claim,"
            "evidence,numbers,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (item_id, c.get("kind"), c.get("speaker"), c.get("via"), c.get("sector"),
             json.dumps(c.get("tickers") or []), c.get("debate"), c.get("stance"),
             c.get("confidence"), c.get("claim"), c.get("evidence_quote"),
             json.dumps(c.get("numbers") or []), datetime.utcnow().isoformat(timespec="seconds")))

    def claims_since(self, since: datetime) -> list[sqlite3.Row]:
        return self.q(
            "SELECT c.*, i.url, i.title AS item_title, i.source_name, i.source_type, i.kind AS item_kind, i.published, "
            "i.is_subscriber, i.repeat_of "
            "FROM claims c JOIN items i ON i.id=c.item_id WHERE c.created>=? OR i.published>=? ORDER BY c.id",
            (since.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"),
             since.astimezone(timezone.utc).isoformat(timespec="seconds")))

    # Usage ------------------------------------------------------------------
    def log_usage(self, provider: str, model: str, inp: int, out: int, cost: float, purpose: str) -> None:
        self.ex("INSERT INTO llm_usage VALUES (?,?,?,?,?,?,?)",
                (datetime.utcnow().isoformat(timespec="seconds"), provider, model, inp, out, cost, purpose))

    def month_spend(self, provider: str = "anthropic") -> float:
        start = date.today().replace(day=1).isoformat()
        row = self.one("SELECT COALESCE(SUM(cost_usd),0) s FROM llm_usage WHERE provider=? AND ts>=?",
                       (provider, start))
        return float(row["s"]) if row else 0.0

    # Source health ----------------------------------------------------------
    def source_ok(self, name: str) -> None:
        self.ex("INSERT INTO source_health(source,last_success) VALUES (?,?) "
                "ON CONFLICT(source) DO UPDATE SET last_success=excluded.last_success",
                (name, datetime.utcnow().isoformat(timespec="seconds")))

    def source_error(self, name: str, err: str) -> None:
        now = datetime.utcnow().isoformat(timespec="seconds")
        self.ex("INSERT INTO source_health(source,last_error,last_error_at) VALUES (?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET last_error=excluded.last_error, "
                "last_error_at=excluded.last_error_at", (name, err[:500], now))

    # Runs -------------------------------------------------------------------
    def start_run(self, kind: str) -> int:
        return self.ex("INSERT INTO runs(kind,started,status) VALUES (?,?,?)",
                       (kind, datetime.utcnow().isoformat(timespec="seconds"), "running"))

    def finish_run(self, run_id: int, status: str, notes: str = "") -> None:
        self.ex("UPDATE runs SET finished=?, status=?, notes=? WHERE id=?",
                (datetime.utcnow().isoformat(timespec="seconds"), status, notes[:4000], run_id))
