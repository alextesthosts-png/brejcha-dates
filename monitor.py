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

URL = 'https://www.borisbrejcha.de/dates'
STATE = Path('data/events.json')
DEBUG = Path('data/debug.html')
DATE_RE = re.compile(r'\b(?:20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]20\d{2}|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+20\d{2})\b', re.I)


def normalize(s):
    return re.sub(r'\s+', ' ', str(s or '')).strip()


def canonical_url(s):
    if not s:
        return ''
    url = urljoin(URL, s)
    parsed = urlparse(url)
    if parsed.scheme not in ('https', 'http'):
        return ''
    return parsed._replace(query='', fragment='').geturl().rstrip('/')


def key_for(event):
    # An event URL alone is not enough: ticketing links can be reused across dates.
    core = '|'.join(normalize(event.get(k)).casefold() for k in ('date', 'title', 'location'))
    return hashlib.sha256(core.encode('utf-8')).hexdigest()[:20]


def extract_jsonld(soup):
    events = []
    def walk(obj):
        if isinstance(obj, list):
            for item in obj:
                walk(item)
        elif isinstance(obj, dict):
            kinds = obj.get('@type', [])
            kinds = [kinds] if isinstance(kinds, str) else kinds
            if any(str(k).lower().endswith('event') for k in kinds):
                loc = obj.get('location') or {}
                if isinstance(loc, list):
                    loc = loc[0] if loc else {}
                if isinstance(loc, dict):
                    addr = loc.get('address') or {}
                    addr = ', '.join(str(addr.get(k, '')) for k in ('addressLocality', 'addressCountry') if addr.get(k)) if isinstance(addr, dict) else str(addr)
                    location = normalize(' '.join(x for x in (loc.get('name', ''), addr) if x))
                else:
                    location = normalize(loc)
                date = normalize(obj.get('startDate'))
                title = normalize(obj.get('name'))
                if date and title:
                    events.append({'date': date, 'title': title, 'location': location, 'url': canonical_url(obj.get('url')) or URL})
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
    """Conservative fallback: event-like containers with a date and event context."""
    events = []
    selectors = '[itemtype*="Event"], [class*="event" i], [class*="tour-date" i], [class*="gig" i], [data-event-id], [data-testid*="event" i]'
    candidates = soup.select(selectors)
    for el in candidates:
        if el.find_parent(attrs={'itemtype': re.compile('Event', re.I)}):
            continue
        text = normalize(el.get_text(' ', strip=True))
        if not 12 <= len(text) <= 600:
            continue
        date_tag = el.select_one('time[datetime], [itemprop="startDate"], [data-date]')
        date = normalize((date_tag.get('datetime') or date_tag.get('content') or date_tag.get('data-date')) if date_tag else '')
        if not date:
            match = DATE_RE.search(text)
            date = match.group(0) if match else ''
        if not date:
            continue
        link = el.select_one('a[href]')
        url = canonical_url(link.get('href')) if link else URL
        title_tag = el.select_one('[itemprop="name"], h2, h3, h4, [class*="title" i]')
        title = normalize(title_tag.get_text(' ', strip=True)) if title_tag else text[:160]
        location_tag = el.select_one('[itemprop="location"], [class*="venue" i], [class*="location" i], [class*="city" i]')
        location = normalize(location_tag.get_text(' ', strip=True)) if location_tag else ''
        if title:
            events.append({'date': date, 'title': title, 'location': location, 'url': url})
    return events


def parse_events(html):
    soup = BeautifulSoup(html, 'html.parser')
    events = extract_jsonld(soup)
    if not events:
        events = extract_dom(soup)
    unique = {}
    for event in events:
        event = {k: normalize(v) for k, v in event.items()}
        event['id'] = key_for(event)
        unique[event['id']] = event
    return sorted(unique.values(), key=lambda e: (e['date'], e['title']))


def fetch_html():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale='en-US', user_agent='Mozilla/5.0 (compatible; ConcertMonitor/1.0)')
        try:
            page.goto(URL, wait_until='domcontentloaded', timeout=60000)
            page.wait_for_timeout(7000)
            # Scroll to trigger lazy-loading, then allow network responses to settle.
            for _ in range(4):
                page.mouse.wheel(0, 1600)
                page.wait_for_timeout(800)
            return page.content()
        finally:
            browser.close()


def send_discord(event, webhook):
    content = (f"🎧 **New Boris Brejcha concert detected!**\n"
               f"📅 **Date:** {event['date']}\n"
               f"🎵 **Event:** {event['title'][:180]}\n"
               f"📍 **Location:** {event['location'][:180] or 'See event page'}\n"
               f"🔗 {event['url'] or URL}")
    response = requests.post(webhook, json={'content': content[:1900], 'allowed_mentions': {'parse': []}}, timeout=30)
    if response.status_code == 429:
        delay = min(float(response.json().get('retry_after', 2)), 60)
        time.sleep(delay)
        response = requests.post(webhook, json={'content': content[:1900], 'allowed_mentions': {'parse': []}}, timeout=30)
    response.raise_for_status()


def main():
    webhook = os.getenv('DISCORD_WEBHOOK_URL')
    if not webhook:
        raise RuntimeError('Missing DISCORD_WEBHOOK_URL GitHub Actions secret')
    html = fetch_html()
    events = parse_events(html)
    if not events:
        DEBUG.parent.mkdir(parents=True, exist_ok=True)
        DEBUG.write_text(html, encoding='utf-8')
        raise RuntimeError('No recognizable events extracted. State preserved. Inspect the uploaded debug HTML artifact and update selectors if needed.')
    STATE.parent.mkdir(parents=True, exist_ok=True)
    previous = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else None
    known = set(previous.get('known_ids', [])) if previous else set()
    current = {event['id'] for event in events}
    if previous is None:
        print(f'Initial baseline: {len(events)} concerts recorded, no notifications sent.')
        known = current
    else:
        new_events = [e for e in events if e['id'] not in known]
        print(f'{len(events)} currently found; {len(new_events)} new.')
        for event in new_events:
            send_discord(event, webhook)
            known.add(event['id'])
            # Save after each successful notification to reduce duplicate risk on retry.
            save_state(known, events)
    save_state(known | current, events)


def save_state(known, events):
    STATE.write_text(json.dumps({'known_ids': sorted(known), 'last_seen': datetime.now(timezone.utc).isoformat(), 'current_events': events}, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
