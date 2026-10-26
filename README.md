# Boris Brejcha Concert Monitor

A Python and GitHub Actions monitor for newly published concerts on [Boris Brejcha's official dates page](https://www.borisbrejcha.de/dates). It uses a headless browser to extract events, remembers previously seen concerts, and sends Discord webhook notifications for newly detected events.

## Setup

1. Create a private GitHub repository and upload the contents of this project (including .github/workflows/monitor.yml).
2. In Discord, open the destination channel's Edit Channel → Integrations → Webhooks (or Server Settings → Integrations → Webhooks). Create a webhook and copy its URL.
3. In GitHub, open Settings → Secrets and variables → Actions → New repository secret. Name it DISCORD_WEBHOOK_URL and paste the webhook URL. Never commit the webhook URL.
4. Under Settings → Actions → General → Workflow permissions, select Read and write permissions so the workflow can commit data/events.json. Ensure repository branch protections permit the Actions bot to push to the default branch, or adjust the persistence strategy.
5. Open Actions → Monitor concerts → Run workflow. Check the logs. On the first successful run, existing concerts become the baseline; no Discord messages are sent.
6. Subsequent successful runs check for new events and post them to Discord. The scheduled workflow runs at minute 17 every three hours (UTC); GitHub may delay scheduled runs.

## How detection works

The monitor uses **Python, Playwright, BeautifulSoup, Requests, and GitHub Actions**. It runs on a three-hour schedule and supports manual runs.

### 1. Website rendering and data collection

The `fetch_html()` function launches headless Chromium through Playwright and visits `https://www.borisbrejcha.de/dates` with an `en-US` locale and custom browser user-agent.

1. Navigate using `wait_until="domcontentloaded"` with a 60-second timeout.
2. Wait 7 seconds for JavaScript-rendered content.
3. Scroll down four times by 1,600 pixels, pausing 800 milliseconds between scrolls to trigger lazy loading.
4. Capture the rendered HTML with `page.content()`.
5. Close the browser in a `finally` block, even if an error occurs.

These are fixed waits, not a guarantee that all asynchronous content has loaded.

### 2. Event extraction and parsing

`parse_events()` processes the rendered HTML using BeautifulSoup and tries two strategies in order.

**Primary: JSON-LD structured data.** `extract_jsonld()` reads `<script type="application/ld+json">` elements, recursively traverses JSON objects and arrays, and identifies objects whose `@type` ends with `Event`. It extracts `startDate`, `name`, `location`, and `url`. Location may include a venue name, city, and country. A date and title are required.

**Fallback: event-like DOM containers.** Only when JSON-LD returns *zero* events, `extract_dom()` searches selectors including:

```css
[itemtype*="Event"], [class*="event" i], [class*="tour-date" i],
[class*="gig" i], [data-event-id], [data-testid*="event" i]
```

For each candidate it:

1. Normalizes text and skips candidates shorter than 12 or longer than 600 characters.
2. Reads date attributes such as `datetime`, `content`, or `data-date`, or searches text with a date-matching regular expression.
3. Extracts a title from structured name fields, headings, or title-related classes; otherwise uses up to 160 characters of text.
4. Extracts a venue, location, or city when present.
5. Extracts an associated link, falling back to the dates page.

**The methods are not merged:** if JSON-LD yields even one event, the DOM fallback is skipped. The fallback is heuristic and may require adjustment if the website changes.

### 3. Normalization and event IDs

`normalize()` collapses whitespace and strips surrounding spaces. `canonical_url()` resolves relative URLs against the dates page, permits HTTP(S) only, and removes query parameters, fragments, and trailing slashes.

`key_for()` generates a deterministic ID from the normalized, case-folded combination:

```text
date | title | location
```

It hashes this string with SHA-256 and uses the first 20 hexadecimal characters. Ticket URLs are deliberately excluded because they may change or be reused. Events with the same ID are deduplicated, and the result is sorted by date and title.

**Limitation:** a change to an event's date, title, or location creates a different ID, potentially triggering a second notification for a corrected or rescheduled event.

### 4. Persistent event history

The state file is `data/events.json`:

| Field | Purpose |
| --- | --- |
| `known_ids` | Sorted list of all historically detected event IDs |
| `last_seen` | UTC timestamp of the most recent successful state save |
| `current_events` | Event details extracted during the latest successful check |

On the **first run without a state file**, all extracted events become a baseline and **no Discord notifications are sent**.

On subsequent runs, an event is considered new when its ID is absent from `known_ids`. Historical IDs are retained even if an event temporarily disappears from the website, preventing repeated alerts when it returns unchanged.

### 5. Discord notifications

For each new event, `main()` calls `send_discord(event, webhook)`. The webhook URL comes from the `DISCORD_WEBHOOK_URL` environment variable, normally provided by a GitHub Actions repository secret.

Each message contains a new-concert heading, date, event title, location (or a fallback), and event URL. The Requests library sends a JSON POST with:

- `User-Agent: BorisBrejchaConcertMonitor/1.0`
- `allowed_mentions: {"parse": []}` to suppress automatic mentions
- Message content truncated to 1,900 characters
- A 30-second request timeout
- Up to three total attempts when Discord responds with HTTP 429, using a bounded `retry_after` delay

Non-successful HTTP responses cause an error. The monitor's own error message does not intentionally include the webhook URL. The real `send_discord()` function has been successfully tested through a manual GitHub Actions run using a fictional concert.

### 6. State persistence and duplicate prevention

After each successful Discord notification, the new ID is added to `known_ids`, and `save_state()` writes `data/events.json`. At the end of a successful scan, the state is saved again with all currently extracted IDs.

The GitHub Actions workflow stages and commits `data/events.json` if it changed, including attempting persistence after a partially failed monitor run.

This provides **best-effort deduplication, not exactly-once delivery**. If Discord accepts a notification but the updated state is not committed, the same event might be announced again on the next run.

### 7. GitHub Actions schedule and manual modes

The workflow is `.github/workflows/monitor.yml` and includes:

```yaml
schedule:
  - cron: '17 */3 * * *'
```

This requests runs at approximately **00:17, 03:17, 06:17, 09:17, 12:17, 15:17, 18:17 and 21:17 UTC**. GitHub Actions may delay or occasionally skip scheduled runs.

The `workflow_dispatch` trigger also supports:

| Mode | Behavior |
| --- | --- |
| Normal monitoring | Install dependencies and Chromium, scrape events, notify on new IDs, persist state |
| Basic Discord webhook test | Send a simple message without scraping or changing state |
| Sample concert test | Import the actual `send_discord()` function and send a fictional concert without scraping or changing state |

Both test modes skip the state persistence step. The sample test validates the notification function, **not** the live extraction and comparison logic.

### 8. Failures and diagnostics

- **Zero events:** save the rendered page to `data/debug.html`, raise an error, and leave historical state unchanged. GitHub Actions attempts to upload a `debug-html` artifact retained for three days.
- **Discord failure:** stop processing further events; earlier successful notifications may already be saved locally, and the workflow attempts to commit that state.
- **Missing webhook:** fail with a configuration error.
- **Browser, navigation, or parsing error:** exit nonzero and inspect the GitHub Actions logs.
- **Partial extraction:** a nonzero event count can still omit concerts; the zero-event safeguard does not detect this case.

## Important limitations

The scraper previously extracted an initial baseline of **15 concerts** from the Boris Brejcha dates page. The basic webhook and the actual `send_discord()` function have both been tested successfully in GitHub Actions. These successes do **not** guarantee that every future event will be detected or delivered.

- Website HTML, JSON-LD, JavaScript, or loading behavior may change.
- Fixed delays and scrolling may miss unusually slow or interactive content.
- JSON-LD can contain only a subset of events and prevent the DOM fallback from running.
- Date, title, or location edits may produce a new ID and a repeat notification.
- The monitor detects newly seen events, **not** cancellations or removals.
- Scheduled GitHub Actions jobs can be delayed or skipped.
- Discord delivery and Git commits are not transactional; exactly-once delivery is not guaranteed.
- Concert details are not independently checked against ticketing providers.

If extraction returns zero events, inspect the `debug-html` artifact. If extraction becomes incomplete, review the website structure and consider adapting `extract_jsonld()`, `extract_dom()`, or a suitable underlying event-data endpoint.

## Local use

Install Python dependencies:

```bash
python -m pip install -r requirements.txt
```

Install the Playwright Chromium browser:

```bash
python -m playwright install chromium
```

Set your webhook URL as an environment variable (never commit the URL):

Linux/macOS:

```bash
export DISCORD_WEBHOOK_URL='YOUR_DISCORD_WEBHOOK_URL'
```

Windows PowerShell:

```powershell
$env:DISCORD_WEBHOOK_URL = 'YOUR_DISCORD_WEBHOOK_URL'
```

Run the monitor and tests:

```bash
python monitor.py
python -m unittest discover -s tests
```

## Troubleshooting

| Problem | Possible cause / action |
| --- | --- |
| No events extracted | Download `debug-html`; inspect rendered HTML and loading behavior |
| Fewer events than expected | JSON-LD may contain only a subset and prevent DOM fallback |
| Discord HTTP 401 or 404 | Check whether the webhook still exists; regenerate and update the secret if needed |
| Discord HTTP 403 | Check webhook permissions, validity, and request headers; the monitor uses a custom user-agent |
| Discord HTTP 429 | Rate limiting; the monitor retries with bounded delays |
| Git push HTTP 403 | Check `contents: write`, workflow permissions, and branch protection |
| No scheduled runs | Confirm Actions are enabled and the workflow is on the default branch; inactive public repos may have schedules disabled |
| Duplicate notifications | Event identity fields may have changed, or a prior state commit may have failed |
| Missing notifications | Inspect run logs, extracted events, and `known_ids` |
| Python import or indentation errors | Verify `monitor.py` is complete, including `send_discord()` and `main()` |
| Sample concert test fails | Inspect the `Send sample concert notification` step; it imports `send_discord()` |
| Basic webhook test passes but monitoring fails | Check scraper, parsing, dependencies, and state persistence independently |
