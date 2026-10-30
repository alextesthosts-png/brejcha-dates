# Boris Brejcha Concert Monitor

A Python and GitHub Actions monitor for newly published concerts on [Boris Brejcha's official dates page](https://www.borisbrejcha.de/dates). It uses a headless browser to extract events, remembers previously seen concerts, and sends notifications to **Discord and Telegram** for newly detected events.

## Setup

1. Create a GitHub repository and upload this project, including `.github/workflows/monitor.yml`, `monitor.py`, `requirements.txt`, and `data/events.json` (if already initialized). A public repository exposes its code, commit history, and Actions logs; never commit credentials.
2. **Discord:** In the destination Discord channel, open **Edit Channel → Integrations → Webhooks** (or **Server Settings → Integrations → Webhooks**), create a webhook, and copy its URL.
3. **Telegram:** Open [@BotFather](https://t.me/BotFather), send `/newbot`, and create a bot. Keep its API token private. Open your bot's chat and tap **Start** (or send `/start`).
4. To find your Telegram chat ID, send a message to your bot, then open `https://api.telegram.org/botYOUR_BOT_TOKEN/getUpdates` in your browser. Locate `message.chat.id`. Never publish or share the full URL because it contains your bot token.
5. In **GitHub → Settings → Secrets and variables → Actions**, create these three repository secrets:

   | Secret | Value |
   | --- | --- |
   | `DISCORD_WEBHOOK_URL` | Discord channel webhook URL |
   | `TELEGRAM_BOT_TOKEN` | Telegram bot API token |
   | `TELEGRAM_CHAT_ID` | Telegram private chat, group, or channel ID |

6. Under **Settings → Actions → General → Workflow permissions**, allow **Read and write permissions** so the workflow can commit `data/events.json`. Branch protection must also allow the Actions bot to push, or persistence must be adapted.
7. Go to **Actions → Monitor concerts → Run workflow**. To verify Telegram, select **only `test_telegram`**; the fictional test does not scrape the website or change concert history. Other tests are described below.
8. Run again with **all test options unchecked** for a normal check. If no state file exists, the first successful run establishes a baseline without notifications. If a state file already exists, previously known concerts remain known.
9. The scheduled workflow runs at minute 17 every three hours (UTC); GitHub may delay scheduled runs.

### Send alerts to a Telegram group instead of a private chat

1. Create a Telegram group and add the bot as a member. Ensure it can post messages.
2. Send a command such as `/start@YourBotUsername` in the group.
3. Open the bot's `getUpdates` URL and find the entry whose `chat.title` matches the group. Copy its `chat.id` (typically a negative number, often beginning `-100`). If no update appears, send another command addressed to the bot and retry.
4. Replace the GitHub secret `TELEGRAM_CHAT_ID` with that **group** chat ID.
5. Run the `test_telegram` workflow option and confirm the fictional alert appears in the group.

**Current limitation:** The current implementation uses **one** `TELEGRAM_CHAT_ID`, so changing it to a group ID redirects Telegram notifications from the private chat to the group. Sending to a group **and** individual users simultaneously, or to several private chats, requires code and delivery-tracking changes. A Telegram channel is another option for one-to-many announcements if the bot has permission to post.

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
| `known_ids` | Sorted list of all historically detected event IDs that have been fully processed |
| `last_seen` | UTC timestamp of the most recent state save |
| `current_events` | Event details extracted during the latest successful check |
| `pending_notifications` | Events awaiting delivery to one or both notification services, including which services have already accepted each alert |

On the **first run without a state file**, all extracted events become a baseline and **no Discord or Telegram notifications are sent**.

On subsequent runs, an event is considered new when its ID is absent from `known_ids`. Historical IDs are retained even if an event disappears from the website, preventing repeated alerts when it returns unchanged. Existing historical events are **not** automatically resent to Telegram merely because Telegram was added later.

### 5. Discord and Telegram notifications

For each new event, `main()` sends a concert announcement containing its date, title, location (or a fallback), and URL.

**Discord:** `send_discord(event, webhook)` uses the `DISCORD_WEBHOOK_URL` secret and sends a JSON POST with:

- `User-Agent: BorisBrejchaConcertMonitor/1.0`
- `allowed_mentions: {"parse": []}` to suppress automatic mentions
- Message content truncated to 1,900 characters
- A 30-second request timeout
- Up to three total attempts for HTTP 429, using a bounded `retry_after` delay

**Telegram:** `send_telegram(event, bot_token, chat_id)` uses `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. It calls Telegram's `sendMessage` Bot API method, requests a normal (not silently disabled) notification, and disables link previews. It uses a 30-second timeout, retries HTTP 429 up to three total attempts using Telegram's `parameters.retry_after`, and checks both the HTTP response and Telegram's JSON `ok` field. Errors are reported without deliberately printing the token-bearing API URL.

The bot can message a private chat after its recipient has started the bot, or a group/channel where the bot has posting access. **Telegram push alerts still depend on each recipient's app, chat mute settings, and device notification settings.**

### 6. State persistence and duplicate prevention

For a newly detected event, the monitor adds an entry to `pending_notifications` and saves the state locally. It then:

1. Sends to Discord unless that destination is already recorded as delivered.
2. Saves the successful Discord delivery marker.
3. Sends to Telegram unless that destination is already recorded as delivered.
4. Saves the successful Telegram delivery marker.
5. Only after both destinations succeed, adds the event ID to `known_ids` and removes the pending entry.

A pending event is retried on a later run even if it has temporarily disappeared from the live dates page. This avoids intentionally resending to a destination that already succeeded when the other destination failed.

The GitHub Actions workflow attempts to commit `data/events.json` after monitoring, **including after a failed monitoring step**, if the state file changed.

This provides **best-effort deduplication, not exactly-once delivery**. Local state saves are not durable across GitHub-hosted runs until committed and pushed. If a service accepts an alert but the run fails before the updated state reaches GitHub, the alert may be sent again. A push failure can also prevent retry markers from persisting.

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
| Basic Discord webhook test (`test_discord`) | Send a simple message without scraping or changing state |
| Sample concert test (`test_concert`) | Import `send_discord()` and send a fictional Discord concert without scraping or changing state |
| Telegram sample test (`test_telegram`) | Import `send_telegram()` and send a fictional Telegram concert without scraping or changing state |

All three test modes skip state persistence, and **only one test checkbox may be selected per run**. The sample tests validate notification delivery, **not** live extraction or event comparison. A normal run has all three test options unchecked. The workflow uses `ubuntu-24.04` to avoid Chromium installation problems encountered on Ubuntu 26.04 and has a 12-minute job timeout.

### 8. Failures and diagnostics

- **Zero events:** save the rendered page to `data/debug.html`, raise an error, and leave historical state unchanged. GitHub Actions attempts to upload a `debug-html` artifact retained for three days.
- **Discord or Telegram failure:** stop processing further events; successful deliveries may already be saved locally in `pending_notifications`, and the workflow attempts to commit that state.
- **Missing credentials:** fail if `DISCORD_WEBHOOK_URL`, `TELEGRAM_BOT_TOKEN`, or `TELEGRAM_CHAT_ID` is missing.
- **Browser, navigation, or parsing error:** exit nonzero and inspect the GitHub Actions logs.
- **Partial extraction:** a nonzero event count can still omit concerts; the zero-event safeguard does not detect this case.

## Important limitations

The scraper previously extracted an initial baseline of **15 concerts** from the Boris Brejcha dates page. The basic Discord webhook, `send_discord()`, and a fictional Telegram notification have been tested successfully through GitHub Actions. These successes do **not** guarantee that every future event will be detected or delivered.

- Website HTML, JSON-LD, JavaScript, or loading behavior may change.
- Fixed delays and scrolling may miss unusually slow or interactive content.
- JSON-LD can contain only a subset of events and prevent the DOM fallback from running.
- Date, title, or location edits may produce a new ID and a repeat notification.
- The monitor detects newly seen events, **not** cancellations or removals.
- Scheduled GitHub Actions jobs can be delayed or skipped.
- Discord and Telegram delivery and Git commits are not transactional; exactly-once delivery is not guaranteed.
- A Telegram bot cannot initiate a private conversation with a user who has not started it.
- The current code sends Telegram notifications to one configured chat ID, not a list of recipients.
- WhatsApp notification delivery is **not implemented** in this repository.
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

Set all three credentials as environment variables (never commit them):

Linux/macOS:

```bash
export DISCORD_WEBHOOK_URL='YOUR_DISCORD_WEBHOOK_URL'
export TELEGRAM_BOT_TOKEN='YOUR_TELEGRAM_BOT_TOKEN'
export TELEGRAM_CHAT_ID='YOUR_TELEGRAM_CHAT_ID'
```

Windows PowerShell:

```powershell
$env:DISCORD_WEBHOOK_URL = 'YOUR_DISCORD_WEBHOOK_URL'
$env:TELEGRAM_BOT_TOKEN = 'YOUR_TELEGRAM_BOT_TOKEN'
$env:TELEGRAM_CHAT_ID = 'YOUR_TELEGRAM_CHAT_ID'
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
| Telegram test fails | Check `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and whether the bot has been started or added to the destination chat |
| Telegram message arrives but no Android push alert | Check Telegram chat mute settings, app notifications, and Android notification permissions |
| Telegram HTTP 429 | Telegram rate limiting; the monitor retries using `retry_after` |
| Group receives no messages | Check the group chat ID (usually negative), bot membership, and posting permissions |
| Git push HTTP 403 | Check `contents: write`, workflow permissions, and branch protection |
| No scheduled runs | Confirm Actions are enabled and the workflow is on the default branch; inactive public repos may have schedules disabled |
| Duplicate notifications | Event identity fields may have changed, or a prior state commit may have failed |
| Missing notifications | Inspect run logs, extracted events, `known_ids`, and `pending_notifications` |
| Python import or indentation errors | Verify `monitor.py` is complete, including `send_discord()`, `send_telegram()`, and `main()` |
| Sample concert test fails | Inspect the `Send sample concert notification` step; it imports `send_discord()` |
| Telegram test passes but normal monitoring fails | Check Discord configuration, scraper, event extraction, and state persistence |
| Basic webhook test passes but monitoring fails | Check scraper, parsing, dependencies, and state persistence independently |
