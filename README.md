# Boris Brejcha Concert Monitor

Monitors https://www.borisbrejcha.de/dates every six hours with GitHub Actions and posts newly detected worldwide concerts to Discord.

## Setup

1. Create a **private** GitHub repository and upload the contents of this project (including `.github/workflows/monitor.yml`).
2. In Discord, open the destination channel's **Edit Channel → Integrations → Webhooks** (or Server Settings → Integrations → Webhooks). Create a webhook and copy its URL.
3. In GitHub, open **Settings → Secrets and variables → Actions → New repository secret**. Name it `DISCORD_WEBHOOK_URL` and paste the webhook URL. Never commit the webhook URL.
4. Under **Settings → Actions → General → Workflow permissions**, select **Read and write permissions** so the workflow can commit `data/events.json`. Ensure repository branch protections permit the Actions bot to push to the default branch, or adjust the persistence strategy.
5. Open **Actions → Monitor concerts → Run workflow**. Check the logs. On the first successful run, existing concerts become the baseline; no Discord messages are sent.
6. Subsequent successful runs check for new events and post them to Discord. The scheduled workflow runs at minute 17 every six hours (UTC); GitHub may delay scheduled runs.

## How detection works

- Chromium renders the website and scrolls to trigger lazy-loaded content.
- First tries structured `schema.org` JSON-LD Event data, then event-like DOM containers with dates.
- Uses a stable ID based on date, event name and location, independent of ticket URL changes.
- Stores **all historically seen IDs**, so temporarily removed and restored events don't cause repeat alerts.
- If zero events are found, fails safely without overwriting state, and uploads `data/debug.html` for inspection.
- Saves state after each successful Discord post. The workflow commits state even after partial failures; this is best-effort deduplication, not an exactly-once delivery guarantee.

## Important limitation

The live site's actual event DOM/API was not verifiable in the build environment. The extractor is a tested general-purpose starting point, **not a confirmed working site-specific extractor**. If the first run reports zero events, download the `debug-html` artifact from the failed GitHub Actions run and inspect where the site loads dates. You may need to adapt `extract_dom()` or extract the site's underlying JSON endpoint. Do not treat a failed first run as evidence of zero concerts.

## Local use

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
export DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...' # use a local .env or environment, never commit it
python monitor.py
python -m unittest discover -s tests
```

## Troubleshooting

- **No events extracted:** inspect `debug-html`; dates may be loaded through an API, embedded iframe, or different HTML structure.
- **Discord 401/404:** recreate the webhook and update the GitHub secret.
- **Git push 403:** enable read/write workflow permissions and review branch protection.
- **No scheduled runs:** check that Actions are enabled and the workflow is on the default branch. GitHub can disable schedules for inactive public repositories.
- **False positives:** improve parsing so the title and location remain stable; current fallback selectors are conservative but may need tailoring.

Please respect the website's access rules and avoid excessive request frequency.
