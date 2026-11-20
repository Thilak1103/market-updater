# Market Updater

A daily weekday finance briefing, published to Notion by 06:45 London time.

It explains what moved markets and why, pitches at least three pair trades a day with a target, stop and horizon, tracks every idea (and the ones you tick to follow) to its outcome, and reviews earnings calls.

- Plan: see the "Market Updater — Daily Finance Briefing Plan" doc
- Settings live in Notion (Sectors, Sources, Debates, Settings); `config/briefing.yaml` is the seed and nightly backup
- Secrets go in a local `.env` file (see `.env.example`); never commit it

Trade ideas are ideas to research, not investment advice.

## Getting it running

See [SETUP.md](SETUP.md): keys, the Notion connection and turning on the daily GitHub Actions run.

## How a day runs

1. **05:00 collect**: 88 feeds and pages (Substacks, research, central banks, headlines, short-seller pages, podcasts), news mentions for people without feeds, your newsletter inbox and the Notion Inbox. Podcasts are transcribed with Groq Whisper, and claims are extracted with Claude Haiku. Each claim must quote its source exactly.
2. **06:10 write**: Prices come from yfinance (cross-checked against Stooq), plus FRED macro, the earnings calendar, reactions and transcripts. Claude Sonnet writes the sections and Claude Opus proposes pair trades. Code then checks every pair's correlation and hedge ratio, builds a comps table from ROIC.ai and Yahoo, and drops any sentence whose numbers can't be traced to a source.
3. **By 06:45 publish**: The briefing goes to Notion and the "Today's briefing" box on the Market Updater page is pointed at it. Each core sector also gets a reading page (articles, Substack posts, research and podcasts worth your time, best first), and each new idea gets its own page in the Ideas database.

### What the briefing contains

| Section | What's in it |
|---|---|
| Top line | The day in one sentence, the main index moves, and 4–6 bullets on what drove markets and why |
| The numbers that matter | Indices, oil, gold, yields, credit spreads and fresh data releases, each with a "so what" |
| Trade ideas | At least three pairs. Each card argues both legs and gives entry prices, a target, a stop, a 20-trading-day horizon, conviction, slim comps (EV/EBITDA, EV/Sales, P/E) and a spread chart. Then a box tracking the ideas you follow |
| Your sectors | Two or three lines per core sector on the big moves and why, linking to that day's reading page |
| Earnings reviews | A verdict per company, with the full review in a toggle |
| Elsewhere | A table: sector, move, what's behind it |
| Consensus check | Where the crowd leans on each live debate, how that is changing and the voice worth knowing |
| Deep dive | Tuesday to Friday: why the previous day's busiest Elsewhere sector moved, and whether it lasts |
| Scorecard | Fridays: every closed idea against the ones you followed |

### Trade idea tracking

Targets, stops and horizons are set in code from the pair's spread (ln long − hedge ratio × ln short). The stop is 1.5× the spread's 60-day volatility below entry. The target is the 60-day average when the spread sits well below it, otherwise 2× the volatility above entry. The horizon is 20 trading days. Tick **Following** on an idea (in the Trade idea dashboard or the Ideas database) and it appears in *My book* and in the daily box; every morning the run updates its return, best and worst so far, progress to target and outcome.

### One-off jobs

Actions → **maintenance** → Run workflow runs `sync-sources` (copies feed URLs from `config/briefing.yaml` into the Notion Sources table), `check-sources` or `export-settings`.

## Layout

| Path | What it does |
|---|---|
| `market_updater/ingest/` | RSS, web pages, email, the Notion Inbox, Google News and podcast transcription |
| `market_updater/analysis/` | Claim extraction, stance tracking and consensus, number and citation checks |
| `market_updater/market/` | Prices, macro, earnings, fundamentals, pair statistics, comps and the snapshot |
| `market_updater/compose/` | Section writing, trade ideas and earnings reviews |
| `market_updater/publish/` | Notion client, markdown to Notion blocks, pages and Telegram alerts |
| `market_updater/run.py` | The daily, collect, weekly and health runs |
| `deploy/` | systemd timers and the Oracle setup script |
| `tests/` | Unit tests and an offline end-to-end run (`python -m pytest`) |
