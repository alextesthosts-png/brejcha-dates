
"""Monitor Boris Brejcha tour dates; notify only about newly discovered events."""

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


URL = "https://www.borisbrejcha.de/dates"
STATE = Path("data/events.json")
DEBUG = Path("data/debug.html")

DATE_RE = re.compile(
    r"\b(?:20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|"
    r"\d{1,2}[-/.]\d{1,2}[-/.]20\d{2}|"
    r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"[a-z]*\s+20\d{2})\b",
    re.I,
)


def normalize(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def canonical_url(s):
    if not s:
        return ""
    url = urljoin(URL, s)
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        return ""
    return parsed._replace(query="", fragment="").geturl().rstrip("/")


def key_for(event):
    core = "|".join(
        normalize(event.get(k)).casefold()
        for k in ("date", "title", "location")
    )
    return hashlib.sha256(core.encode("utf-8")).hexdigest()[:20]


def entity_name(value):
    """Use only names explicitly present in the source structured data."""
    if isinstance(value, list):
        return entity_name(value[0]) if value else ""
    if isinstance(value, dict):
        return normalize(value.get("name"))
    return normalize(value)


def ticket_link(obj):
    """Only publish an explicit offer URL; never guess a ticket seller."""
    offers = obj.get("offers") or []
    if isinstance(offers, dict):
        offers = [offers]
    if not isinstance(offers, list):
        return ""
    for offer in offers:
        if isinstance(offer, dict):
            url = canonical_url(offer.get("url"))
            if url:
                return url
    return ""


def extract_jsonld(soup):
    events = []

    def walk(obj):
        if isinstance(obj, list):
            for item in obj:
                walk(item)

        elif isinstance(obj, dict):
            kinds = obj.get("@type", [])
            kinds = [kinds] if isinstance(kinds, str) else kinds

            if any(str(k).lower().endswith("event") for k in kinds):
                loc = obj.get("location") or {}

                if isinstance(loc, list):
                    loc = loc[0] if loc else {}

                if isinstance(loc, dict):
                    addr = loc.get("address") or {}

                    if isinstance(addr, dict):
                        addr = ", ".join(
                            str(addr.get(k, ""))
                            for k in ("addressLocality", "addressCountry")
                            if addr.get(k)
                        )
                    else:
                        addr = str(addr)

                    location = normalize(
                        " ".join(
                            x for x in (loc.get("name", ""), addr) if x
                        )
                    )
                else:
                    location = normalize(loc)

                date = normalize(obj.get("startDate"))
                title = normalize(obj.get("name"))

                if date and title:
                    events.append({
                        "date": date,
                        "title": title,
                        "location": location,
                        "url": canonical_url(obj.get("url")) or URL,
                        "ticket_url": ticket_link(obj),
                        "organizer": entity_name(obj.get("organizer")),
                    })

            for val in obj.values():
                if isinstance(val, (dict, list)):
                    walk(val)

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            walk(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            continue

    return events


def extract_dom(soup):
    """Fallback: find event containers containing dates."""
    events = []

    selectors = (
        '[itemtype*="Event"], '
        '[class*="event" i], '
        '[class*="tour-date" i], '
        '[class*="gig" i], '
        '[data-event-id], '
        '[data-testid*="event" i]'
    )

    candidates = soup.select(selectors)

    for el in candidates:
        if el.find_parent(
            attrs={"itemtype": re.compile("Event", re.I)}
        ):
            continue

        text = normalize(el.get_text(" ", strip=True))

        if not 12 <= len(text) <= 600:
            continue

        date_tag = el.select_one(
            'time[datetime], [itemprop="startDate"], [data-date]'
        )

        date = normalize(
            (
                date_tag.get("datetime")
                or date_tag.get("content")
                or date_tag.get("data-date")
            ) if date_tag else ""
        )

        if not date:
            match = DATE_RE.search(text)
            date = match.group(0) if match else ""

        if not date:
            continue

        link = el.select_one("a[href]")
        url = canonical_url(link.get("href")) if link else URL

        title_tag = el.select_one(
            '[itemprop="name"], h2, h3, h4, [class*="title" i]'
        )

        title = (
            normalize(title_tag.get_text(" ", strip=True))
            if title_tag else text[:160]
        )

        location_tag = el.select_one(
            '[itemprop="location"], '
            '[class*="venue" i], '
            '[class*="location" i], '
            '[class*="city" i]'
        )

        location = (
            normalize(location_tag.get_text(" ", strip=True))
            if location_tag else ""
        )

        if title:
            events.append({
                "date": date,
                "title": title,
                "location": location,
                "url": url,
                "ticket_url": "",
                "organizer": "",
            })

    return events


def parse_events(html):
    soup = BeautifulSoup(html, "html.parser")
    events = extract_jsonld(soup)

    if not events:
        events = extract_dom(soup)

    unique = {}

    for event in events:
        event = {k: normalize(v) for k, v in event.items()}
        event["id"] = key_for(event)
        unique[event["id"]] = event

    return sorted(
        unique.values(),
        key=lambda e: (e["date"], e["title"]),
    )


def fetch_html():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        page = browser.new_page(
            locale="en-US",
            user_agent="Mozilla/5.0 (compatible; ConcertMonitor/1.0)",
        )

        try:
            page.goto(
                URL,
                wait_until="domcontentloaded",
                timeout=60000,
            )

            page.wait_for_timeout(7000)

            for _ in range(4):
                page.mouse.wheel(0, 1600)
                page.wait_for_timeout(800)

            return page.content()

        finally:
            browser.close()


def send_discord(event, webhook):
    """Send one real or sample concert notification."""

    content = (
        f"🎧 **New Boris Brejcha concert detected!**\n"
        f"📅 **Date:** {event['date']}\n"
        f"🎵 **Event:** {event['title'][:180]}\n"
        f"📍 **Location:** "
        f"{event['location'][:180] or 'See event page'}\n"
        f"🔗 {event['url'] or URL}"
    )

    payload = {
        "content": content[:1900],
        "allowed_mentions": {"parse": []},
    }

    headers = {
        "User-Agent": "BorisBrejchaConcertMonitor/1.0",
    }

    for attempt in range(3):
        response = requests.post(
            webhook,
            json=payload,
            headers=headers,
            timeout=30,
        )

        if response.status_code == 429 and attempt < 2:
            try:
                delay = float(
                    response.json().get("retry_after", 2)
                )
            except (ValueError, TypeError, AttributeError):
                delay = 2

            time.sleep(max(0, min(delay, 60)))
            continue

        if not response.ok:
            # Do not print the webhook URL: it contains a secret.
            raise RuntimeError(
                "Discord rejected notification "
                f"(HTTP {response.status_code})."
            )

        print("Discord notification delivered successfully.")
        return


def send_telegram(event, bot_token, chat_id):
    """Send a concert notification using the Telegram Bot API."""
    message = (
        "🎧 New Boris Brejcha concert detected!\\n"
        f"📅 Date: {event['date']}\\n"
        f"🎵 Event: {event['title'][:180]}\\n"
        f"📍 Location: {event['location'][:180] or 'See event page'}\\n"
        f"🔗 {event['url'] or URL}"
    )
    # Telegram's Bot API returns HTTP 200 with {"ok": false} in some errors.
    # Avoid including the token-bearing URL in exception messages.
    for attempt in range(3):
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "disable_notification": False,
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
        except requests.RequestException:
            raise RuntimeError("Telegram request failed (network or timeout).") from None

        if response.status_code == 429 and attempt < 2:
            try:
                delay = float(response.json().get("parameters", {}).get("retry_after", 2))
            except (ValueError, TypeError, AttributeError):
                delay = 2
            time.sleep(max(0, min(delay, 60)))
            continue

        try:
            accepted = response.json().get("ok") is True
        except (ValueError, TypeError, AttributeError):
            accepted = False

        if not response.ok or not accepted:
            raise RuntimeError(
                f"Telegram rejected notification (HTTP {response.status_code})."
            )
        print("Telegram notification delivered successfully.")
        return


def save_state(known, events, pending=None, first_seen=None):
    """Keep existing state fields and track incomplete multi-channel delivery."""
    STATE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "known_ids": sorted(known),
        "last_seen": datetime.now(timezone.utc).isoformat(),
        "current_events": events,
        "pending_notifications": pending or {},
        "first_seen": first_seen or {},
    }
    STATE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main():
    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not webhook:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL GitHub Actions secret")
    if not bot_token or not chat_id:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID GitHub Actions secret"
        )

    html = fetch_html()
    events = parse_events(html)

    if not events:
        DEBUG.parent.mkdir(parents=True, exist_ok=True)
        DEBUG.write_text(html, encoding="utf-8")
        raise RuntimeError(
            "No recognizable events extracted. State preserved. Inspect debug HTML."
        )

    previous = (
        json.loads(STATE.read_text(encoding="utf-8"))
        if STATE.exists()
        else None
    )
    current = {event["id"] for event in events}

    if previous is None:
        print(f"Initial baseline: {len(events)} concerts recorded, no notifications sent.")
        save_state(current, events, first_seen={})
        return

    known = set(previous.get("known_ids", []))
    pending = previous.get("pending_notifications", {})
    if not isinstance(pending, dict):
        raise RuntimeError("Invalid pending_notifications in state file.")

    first_seen = previous.get("first_seen", {})
    if not isinstance(first_seen, dict):
        raise RuntimeError("Invalid first_seen in state file.")
    # Older concerts stay historical: do not invent a discovery timestamp.
    discovered_at = datetime.now(timezone.utc).isoformat()
    new_events = [event for event in events if event["id"] not in known]
    for event in new_events:
        first_seen.setdefault(event["id"], discovered_at)
    for event_id in pending:
        first_seen.setdefault(event_id, discovered_at)

    print(f"{len(events)} currently found; {len(new_events)} not yet fully notified.")

    # Retain previously pending events even if temporarily removed from the page.
    for event in new_events:
        pending.setdefault(event["id"], {"event": event, "delivered": []})

    # Persist the queue before making network requests.
    save_state(known, events, pending, first_seen)

    for event_id in list(pending):
        item = pending[event_id]
        event = item["event"]
        delivered = set(item.get("delivered", []))

        if "discord" not in delivered:
            send_discord(event, webhook)
            delivered.add("discord")
            item["delivered"] = sorted(delivered)
            save_state(known, events, pending, first_seen)

        if "telegram" not in delivered:
            send_telegram(event, bot_token, chat_id)
            delivered.add("telegram")
            item["delivered"] = sorted(delivered)
            save_state(known, events, pending, first_seen)

        # Mark fully processed only after both destinations accept the event.
        known.add(event_id)
        del pending[event_id]
        save_state(known, events, pending, first_seen)

    save_state(known, events, pending, first_seen)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
