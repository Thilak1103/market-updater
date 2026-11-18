# Setting up Market Updater

The code is finished and tested. To start getting briefings, it needs a server, its keys and a Notion connection. Expect about 45 minutes, and none of it costs money.

## 1. Gather the keys

Put each key in Apple Passwords as you get it. You'll copy them into the server in step 3.

| Key | Where | Status |
|---|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com → API keys → Create key (name it "market-updater") | To do |
| `GROQ_API_KEY` | console.groq.com → API keys | Saved |
| `FRED_API_KEY` | fredaccount.stlouisfed.org → API keys | Saved |
| `FINNHUB_API_KEY` | finnhub.io dashboard | Saved |
| `ROIC_API_KEY` | roic.ai → account → API | Saved |
| `ALPHAVANTAGE_API_KEY` | alphavantage.co | Saved |
| `NOTION_TOKEN` | step 2 | To do |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Optional: failure alerts on your phone (step 5) | Optional |

On the Anthropic console, set a monthly spend limit of $100 under Settings → Limits so the Max-plan credit can't be exceeded. The code also stops using Claude at $80 a month and switches to Groq.

## 2. Connect Notion

1. Go to notion.so/profile/integrations and choose **New integration**. Name it "Market Updater", pick your workspace, and leave the type as Internal.
2. Copy the **Internal Integration Secret**. This is `NOTION_TOKEN`.
3. In Notion, open the **Market Updater** page. Click **⋯** → **Connections** → add "Market Updater". This gives the integration access to all nine databases under that page.

The database IDs are already in `config/briefing.yaml`.

## 3. Create the server (Oracle Cloud Always Free)

1. Sign up at signup.cloud.oracle.com. It asks for a card for identity checks; Always Free resources are never charged. Pick **UK South (London)** as your home region.
2. Go to **Compute → Instances → Create instance**:
   - Image: **Ubuntu 24.04**.
   - Shape: **VM.Standard.A1.Flex** (Ampere) with 1 OCPU and 6 GB memory. If Ampere capacity is unavailable, use **VM.Standard.E2.1.Micro**.
   - Under **Add SSH keys**, download the private key.
3. Connect: `ssh -i <downloaded-key> ubuntu@<public-ip>`
4. On the server, run:
   ```bash
   git clone https://github.com/<you>/market-updater.git
   cd market-updater
   bash deploy/setup_oracle.sh        # first run installs everything and creates .env
   nano /opt/market-updater/.env      # paste the keys from step 1
   bash deploy/setup_oracle.sh        # second run checks sources and turns on the timers
   ```
   If the repo is private, create a GitHub fine-grained token with read access to that one repo, and use it as the password when cloning.
5. Test it now, without waiting for tomorrow:
   ```bash
   cd /opt/market-updater
   .venv/bin/python -m market_updater collect --force
   .venv/bin/python -m market_updater run --force
   ```
   A page should appear in the Notion Briefings database within about 20 minutes.

The timers run on London time, so they handle BST and GMT automatically:

| When | What |
|---|---|
| Weekdays 05:00 | Collect sources, transcribe podcasts, extract claims |
| Weekdays 06:10 | Market snapshot, write the sections and ideas, publish to Notion |
| Weekdays 07:00 | Health check; sends an alert if there's no page |
| Sundays 18:00 | Earnings calendar, fundamentals refresh, Substack discovery |
| Nightly 23:30 | Back up the Notion settings to `data/settings_backup/` |

## 4. Day-to-day editing (all in Notion)

| To do this | Do this in Notion |
|---|---|
| Change sectors, watch lists or proxies | Edit the **Sectors** table |
| Add or pause a source | Edit **Sources**, or drop a link in **Inbox** tagged "Add source" |
| Get a webcast or podcast episode transcribed | Drop the link in **Inbox** tagged "Include" |
| Change thresholds, word counts or ideas per day | Edit the **Settings** table |
| Track or ignore a new Substack | Mark it in **Discovery** |
| Accept a suggested debate | Edit the **Debates** table |

Changes apply from the next run. If Notion can't be reached, the run falls back to `config/briefing.yaml`.

## 5. Optional: failure alerts on Telegram

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token. This is `TELEGRAM_BOT_TOKEN`.
2. Send your new bot any message. Then open `https://api.telegram.org/bot<token>/getUpdates` and copy `chat.id`. This is `TELEGRAM_CHAT_ID`.
3. Add both to `.env`.

Alerts only fire when a run fails or finishes partially.

## Useful commands

```bash
cd /opt/market-updater
.venv/bin/python -m market_updater check-sources        # which feeds work right now
.venv/bin/python -m market_updater dry-run --force      # build today's page into data/out/ without publishing
journalctl -u market-updater-daily -n 200               # logs from the last run
git pull && .venv/bin/pip install -r requirements.txt   # update the code
```

## Fallback: GitHub Actions instead of Oracle

1. Add the keys as repository secrets.
2. Set the repository variable `RUN_ON_ACTIONS` to `true`.
3. `.github/workflows/daily.yml` will run the same two stages.

GitHub's scheduler can start runs 10–30 minutes late, so Oracle is the better option.
