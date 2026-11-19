# Market Updater

A daily weekday finance briefing, published to Notion by 06:45 London time.

It summarises markets and, above all, what named investors, analysts and writers think about them: who disagrees, where consensus is crowded, and where a narrative may be cracking. It also pitches 3–4 pair-trade ideas a day with comps, and reviews earnings calls.

- Plan: see the "Market Updater — Daily Finance Briefing Plan" doc
- Settings live in Notion (Sectors, Sources, Debates, Settings); `config/briefing.yaml` is the seed and nightly backup
- Secrets go in a local `.env` file (see `.env.example`); never commit it

Trade ideas are ideas to research, not investment advice.

## Getting it running

See [SETUP.md](SETUP.md): keys, the Notion connection and turning on the daily GitHub Actions run.

## How a day runs

1. **05:00 collect**: 88 feeds and pages (Substacks, research, central banks, headlines, short-seller pages, podcasts), news mentions for people without feeds, your newsletter inbox and the Notion Inbox. Podcasts are transcribed with Groq Whisper, and claims are extracted with Claude Haiku. Each claim must quote its source exactly.
2. **06:10 write**: Prices come from yfinance (cross-checked against Stooq), plus FRED macro, the earnings calendar, reactions and transcripts. Claude Sonnet writes the sections and Claude Opus proposes pair trades. Code then checks every pair's correlation and hedge ratio, builds a comps table from ROIC.ai and Yahoo, and drops any sentence whose numbers can't be traced to a source.
3. **By 06:45 publish**: The briefing goes to Notion, with views, ideas and proposed debates synced to their databases. On Fridays it adds a deep dive on the busiest "Elsewhere" sector, a trade-idea scorecard and source health.

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
