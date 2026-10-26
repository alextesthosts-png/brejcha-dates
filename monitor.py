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

