# Setting up Market Updater

The code is finished and tested. To start getting briefings, it needs its keys and a Notion connection. Expect about 20 minutes, and none of it costs money.

## 1. Gather the keys

Put each key in Apple Passwords as you get it. You'll paste them into GitHub in step 3.

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

## 3. Turn on the daily run (GitHub Actions, free)

The briefing runs on GitHub's own machines, so you don't need a server. This is free because the repo is public. Your keys stay hidden as encrypted secrets that nobody can see, even on a public repo.

1. On the repo page, go to **Settings → Secrets and variables → Actions**.
2. Click **New repository secret** for each key below. The **Name** must match exactly; paste the key as the **Secret**.
   - `ANTHROPIC_API_KEY`
   - `GROQ_API_KEY`
   - `FRED_API_KEY`
   - `FINNHUB_API_KEY`
   - `ROIC_API_KEY`
   - `ALPHAVANTAGE_API_KEY`
   - `NOTION_TOKEN`
   - Optional: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
3. Test it now. Go to the **Actions** tab, pick **daily briefing**, click **Run workflow**, then **Run workflow** again. A page should appear in Notion in about 20–30 minutes. Click the run to watch the logs.

It then runs on its own (London time; GMT and BST are both handled):

| When | Workflow | What |
|---|---|---|
| Weekdays 05:05 | daily briefing | Collect and extract, wait until 06:10, then write and publish |
| Weekdays 07:00 | health check | Fails (GitHub emails you) and alerts on Telegram if there's no page |
| Sundays 18:00 | weekly maintenance | Earnings calendar, fundamentals, Substack discovery, settings backup |

- **Pausing:** To pause everything, add the repository variable `PAUSE_BRIEFING` = `true` under Settings → Secrets and variables → Actions → **Variables**.
- **Late starts:** GitHub can start scheduled runs 10–30 minutes late at busy times. The run starts at 05:05, so the page normally still arrives before 7.
- **Database:** The run keeps its database in the Actions cache, which is kept as long as it's used at least once a week.
- **Notion Sources table:** Run `python -m market_updater sync-sources` once from any machine with the keys set, or ask Claude to do it, to copy the feed URLs into your Notion Sources table.

### Alternative: your own server

If you later want a server, for no late starts and a database that never expires, `deploy/setup_oracle.sh` works on any Ubuntu 22.04+ machine, such as Oracle's free tier or a Hetzner box at about €4 a month. Clone the repo, run the script, fill in `/opt/market-updater/.env`, and run the script again. If you do this, set `PAUSE_BRIEFING` so the two don't both run.

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
3. Add both as repository secrets (step 3).

Alerts only fire when a run fails or finishes partially.
