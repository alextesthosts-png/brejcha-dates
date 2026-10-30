
"""Monitor Boris Brejcha tour dates; notify only about newly discovered events."""

import hashlib
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.parse import parse_qs, quote_plus, unquote

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


URL = "https://www.borisbrejcha.de/dates"
STATE = Path("data/events.json")
DEBUG = Path("data/debug.html")
TICKET_DEBUG = Path("data/ticket_debug.json")

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
            url = urljoin(URL, str(offer.get("url") or "")) if offer.get("url") else ""
            if url:
                return url
    return ""



# Optional, manually verified event-id -> ticket-provider URL overrides.
# Example: {"20_character_event_id": "https://provider.example/event"}
TICKET_OVERRIDES = Path("data/ticket_overrides.json")


def safe_ticket_url(raw):
    """Only allow public HTTPS URLs; never return a Bandsintown tracking link as direct."""
    try:
        parsed = urlparse(str(raw or "").strip())
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host or host == "localhost":
            return ""
        if host.endswith(".local") or host.endswith(".internal"):
            return ""
        if host in ("bandsintown.com", "www.bandsintown.com") or host.endswith(".bandsintown.com"):
            return ""
        # Reject obvious private IP addresses to avoid fetching internal resources.
        import ipaddress
        try:
            ip = ipaddress.ip_address(host)
            if not ip.is_global:
                return ""
        except ValueError:
            pass
        if parsed.username or parsed.password:
            return ""
        return parsed._replace(fragment="").geturl()
    except (ValueError, TypeError):
        return ""


def resolve_ticket_url(raw):
    """Resolve explicit ticket links; never treat a generic event URL as tickets."""
    return resolve_ticket_url_http(raw)


# Ticket CTA discovery: a Bandsintown /e/ event URL is NOT a ticket URL.
# Visit the event page, find its Tickets/Buy CTA, and follow that link only.
TICKET_CTA_RE = re.compile(r"\b(tickets?|buy tickets?|get tickets?|book tickets?|entradas|billets|karten|boletos)\b", re.I)
TRACKING_RE = re.compile(r"^https://(?:[^/]+\.)?bandsintown\.com/t/", re.I)
EVENT_PAGE_RE = re.compile(r"^https://(?:[^/]+\.)?bandsintown\.com/(?:e/|event/)", re.I)


def ticket_cta_candidates(page):
    """Read actual anchors/buttons from a rendered event page, including frames.

    Do not treat arbitrary outbound links or RSVP controls as ticket links.
    """
    found = []
    for frame in page.frames:
        try:
            items = frame.locator('a[href]').evaluate_all("""els => els.map(a => ({
              href: a.href,
              text: (a.innerText || a.getAttribute('aria-label') || a.getAttribute('title') || '').trim(),
              label: (a.getAttribute('data-testid') || '')
            }))""")
        except Exception:
            continue
        for item in items:
            href = item.get('href', '')
            label = (item.get('text', '') + ' ' + item.get('label', '')).strip()
            if not href.startswith('https://') or not TICKET_CTA_RE.search(label):
                continue
            # Prefer a dedicated ticket-tracking URL; don't take the /e/ details URL.
            if EVENT_PAGE_RE.match(href):
                continue
            rank = 0 if TRACKING_RE.match(href) else 1
            found.append((rank, href))
    return [href for _, href in sorted(set(found))]


def _ticket_candidates_in_frame(frame, frame_index=0):
    """Read ticket CTAs and their nearby event context, without clicking anything."""
    try:
        items = frame.locator('a[href], [role="button"], button, [onclick]').evaluate_all(r"""els => {
          const text = el => (el?.innerText || el?.textContent || '').trim().replace(/\s+/g, ' ');
          const ticket = /\b(ticket(s)?|buy ticket(s)?|get ticket(s)?|entradas|billets|karten|boletos)\b/i;
          const out = [];
          for (const [elementIndex, el] of els.entries()) {
            const label = [text(el), el.getAttribute('aria-label'), el.getAttribute('title'),
                           el.getAttribute('data-testid')].filter(Boolean).join(' ').slice(0, 160);
            if (!ticket.test(label) || /\brsvp\b/i.test(label)) continue;
            const href = el.href || el.getAttribute('data-href') || el.getAttribute('data-url') || '';
            // Select the nearest reasonably sized event row, not the whole widget/page.
            let row = el;
            for (let i = 0; i < 7 && row.parentElement; i++) {
              const parent = row.parentElement;
              const t = text(parent);
              if (t.length > 700) break;
              row = parent;
              if (t.length >= 25 && /\b20\d{2}\b/.test(t)) break;
            }
            const anchors = [...row.querySelectorAll('a[href]')].slice(0, 20).map(a => a.href);
            const times = [...row.querySelectorAll('time[datetime]')].map(t => t.getAttribute('datetime'));
            const attrs = ['data-event-id','data-id','data-date','data-start-date'].map(k => row.getAttribute(k) || '');
            el.setAttribute('data-bb-ticket-v4', String(elementIndex));
            out.push({href, label, context: text(row).slice(0, 500), anchors, times, attrs,
                      element_index: elementIndex});
          }
          return out.slice(0, 120);
        }""")
        for item in items:
            item['frame_index'] = frame_index
        return items
    except Exception:
        return []


def _ticket_href(item):
    """Choose only a ticket CTA URL; retain tracking query parameters."""
    links = [item.get('href', '')]
    links.extend(item.get('anchors', []))
    for raw in links:
        raw = str(raw or '')
        if raw.startswith('/'):
            raw = urljoin(URL, raw)
        if not raw.startswith('https://') or EVENT_PAGE_RE.match(raw):
            continue
        if TRACKING_RE.match(raw) or safe_ticket_url(raw):
            # Never use arbitrary sibling links as tickets: only the CTA itself,
            # unless the CTA is a button with no href and a tracking link nearby.
            if raw == links[0] or (not links[0] and TRACKING_RE.match(raw)):
                return raw
    return ''


def _event_reference_ids(text):
    """Extract Bandsintown event IDs from URLs, not our SHA event IDs."""
    ids = set()
    for value in re.findall(r'https?://[^\s\"<>]+', str(text or '')):
        value = value.rstrip('),;')
        parsed = urlparse(value)
        m = re.search(r'/(?:e|event|t)/(\d+)', parsed.path)  # /t/<id> = ticket tracking link
        if m:
            ids.add(m.group(1))
        for key in ('event_id', 'eventId'):
            ids.update(x for x in parse_qs(parsed.query).get(key, []) if x.isdigit())
    return ids


MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"


def _dates_in_text(text):
    """Return every date found in text as ISO strings (handles 'Aug. 07 2027', '7 Aug 2027',
    '2027-08-07', '07.08.2027')."""
    text = str(text or "")
    found = set()
    for m in re.finditer(_MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b", text, re.I):
        found.add(f"{m.group(3)}-{MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}")
    for m in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MON + r",?\s+(20\d{2})\b", text, re.I):
        found.add(f"{m.group(3)}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}")
    for m in re.finditer(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text):
        found.add(m.group(0))
    for m in re.finditer(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2})\b", text):
        found.add(f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}")
    return found


def _match_official_ctas(events, items):
    """Conservative matching: unique Bandsintown ID, or unique date + venue.

    Ambiguous matches are deliberately skipped rather than assigning a wrong seller.
    """
    matches = {}
    diagnostics = []
    for item in items:
        href = _ticket_href(item)
        if not href:
            diagnostics.append({'result': 'no_ticket_href', 'label': item.get('label', '')})
            continue
        candidates = []
        references = _event_reference_ids(' '.join([item.get('href','')] + item.get('anchors', [])))
        if references:
            candidates = [e for e in events if references & _event_reference_ids(e.get('url', ''))]
        if not candidates:
            context = ' '.join([item.get('context', '')] + item.get('times', []) + item.get('attrs', [])).casefold()
            context_dates = _dates_in_text(context)
            for e in events:
                event_dates = _dates_in_text(e.get('date', '')) or ({e['date'][:10]} if e.get('date') else set())
                if not (event_dates & context_dates):
                    continue
                # Require venue or a distinctive title token; date alone can be ambiguous.
                venue = re.sub(r'^boris brejcha\s*@\s*', '', e.get('title', ''), flags=re.I).strip().casefold()
                location = e.get('location', '').casefold()
                venue_parts = [venue] + [x.strip() for x in re.split(r',', location) if len(x.strip()) >= 5]
                if any(len(v) >= 5 and v in context for v in venue_parts):
                    candidates.append(e)
        if len(candidates) != 1:
            diagnostics.append({'result': 'ambiguous' if candidates else 'unmatched', 'label': item.get('label', ''), 'matches': len(candidates)})
            continue
        event = candidates[0]
        matches.setdefault(event['id'], []).append(href)
        diagnostics.append({'result': 'matched', 'event_id': event['id'], 'label': item.get('label', '')})
    # Multiple different CTA URLs for one event may be legitimate ticket tiers.
    # Only accept an unambiguous single URL.
    matches = {eid: links[0] for eid, links in matches.items() if len(set(links)) == 1}
    return matches, diagnostics


def _load_official_page(page):
    page.goto(URL, wait_until='domcontentloaded', timeout=45000)
    page.wait_for_timeout(5000)
    for _ in range(5):
        page.mouse.wheel(0, 1700)
        page.wait_for_timeout(500)


def _resolve_by_click(context, page, frame_index, element_index):
    """Click the actual ticket CTA and observe its navigation or popup.

    Never click RSVP, checkout, payment, or unrelated links. The caller supplies
    a ticket-labeled CTA already matched uniquely to an event.
    """
    if frame_index >= len(page.frames):
        return '', 'frame_missing'
    frame = page.frames[frame_index]
    locator = frame.locator(f'[data-bb-ticket-v4="{element_index}"]')
    if locator.count() != 1:
        return '', 'button_missing'
    opened = []
    def verified_destination(url):
        candidate = safe_ticket_url(url)
        host = (urlparse(candidate).hostname or '').lower()
        if host in ('borisbrejcha.de', 'www.borisbrejcha.de'):
            return ''
        return candidate
    def capture_popup(new_page):
        opened.append(new_page)
    context.on('page', capture_popup)
    try:
        locator.click(timeout=4500, no_wait_after=True)
        page.wait_for_timeout(1800)
        # Popup may start at about:blank and navigate asynchronously.
        for popup in opened[:3]:
            try:
                popup.wait_for_load_state('domcontentloaded', timeout=3500)
            except Exception:
                pass
            result = verified_destination(popup.url)
            if result:
                return result, 'popup'
        result = verified_destination(page.url)
        if result:
            return result, 'same_tab'
        # Sometimes a ticket CTA navigates inside its iframe.
        for current_frame in page.frames:
            result = verified_destination(current_frame.url)
            if result:
                return result, 'iframe'
        return '', 'click_no_provider'
    except Exception as exc:
        return '', f'click_{type(exc).__name__}'
    finally:
        context.remove_listener('page', capture_popup)
        for popup in opened:
            try:
                popup.close()
            except Exception:
                pass


BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
BROWSER_ARGS = ["--disable-blink-features=AutomationControlled"]
TRACKING_DEBUG = Path("data/tracking_debug.json")
# Interstitial pages sometimes redirect with JS or <meta refresh> instead of an HTTP 302.
JS_REDIRECT_RE = re.compile(
    r"""(?:location(?:\.href)?\s*=\s*|location\.(?:replace|assign)\(\s*|url=)["']?(https?:(?:\\?/){2}[^"'\s<>)]+)""",
    re.I)


def _is_bandsintown(url):
    host = (urlparse(url).hostname or "").lower()
    return host == "bandsintown.com" or host.endswith(".bandsintown.com")


def resolve_tracking_in_browser(context, tracking_url, timeout_ms=20000):
    """Open a Bandsintown /t/ link like a visitor and report where it ends up.

    Returns (direct_url, method, hosts_visited). Waits for the redirect to finish, which the
    old click code did not (it checked the URL after 1.8 s while still on bandsintown.com).
    """
    page = context.new_page()
    visited = []
    page.on("framenavigated", lambda frame: visited.append(frame.url) if frame == page.main_frame else None)
    try:
        try:
            page.goto(tracking_url, wait_until="commit", timeout=timeout_ms)
        except Exception:
            pass  # aborted/slow navigations are fine; we inspect where we ended up
        try:
            page.wait_for_url(lambda u: u.startswith("http") and not _is_bandsintown(u),
                              timeout=timeout_ms, wait_until="commit")
        except Exception:
            pass
        hosts = []
        for u in visited + [page.url]:
            h = urlparse(u).hostname
            if h and h not in hosts:
                hosts.append(h)
        for url in [page.url] + list(reversed(visited)):
            if url.startswith("http") and not _is_bandsintown(url):
                final = safe_ticket_url(url)
                if final:
                    return final, "browser_redirect", hosts
        try:
            html = page.content()
        except Exception:
            html = ""
        for m in JS_REDIRECT_RE.finditer(html):
            target = urljoin(page.url, m.group(1).replace("\\/", "/"))
            if not _is_bandsintown(target):
                final = safe_ticket_url(target)
                if final:
                    return final, "page_redirect", hosts
        low = html.lower()
        if any(w in low for w in ("captcha", "access denied", "verify you are", "are you a robot", "unusual traffic")):
            return "", "blocked_by_bot_check", hosts
        return "", "no_redirect", hosts
    except Exception as exc:
        return "", f"nav_{type(exc).__name__}", []
    finally:
        try:
            page.close()
        except Exception:
            pass


def resolve_tracking_urls(events):
    """Resolve every event that still only has a Bandsintown /t/ link."""
    todo = [e for e in events if not e.get("ticket_url") and TRACKING_RE.match(e.get("ticket_tracking_url", ""))]
    if not todo:
        return
    print(f"Tracking-link resolution: {len(todo)} events")
    debug = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=BROWSER_ARGS)
            context = browser.new_context(locale="en-US", user_agent=BROWSER_UA)
            try:
                for event in todo:
                    direct, method, hosts = resolve_tracking_in_browser(context, event["ticket_tracking_url"])
                    if direct:
                        event["ticket_url"] = direct
                        event["ticket_provider"] = urlparse(direct).hostname or ""
                    print(f"Tracking resolution {event['id']}: {event.get('ticket_provider') or 'unresolved'} ({method})")
                    debug.append({"event_id": event["id"], "method": method, "hosts": hosts})
            finally:
                browser.close()
    except Exception as exc:
        print(f"Tracking resolution unavailable ({type(exc).__name__})")
    TRACKING_DEBUG.parent.mkdir(parents=True, exist_ok=True)
    TRACKING_DEBUG.write_text(json.dumps(debug, indent=2) + "\n", encoding="utf-8")


def discover_official_tickets(events):
    """Match official ticket buttons, then click them in a real browser.

    No external navigation is attempted for unmatched/ambiguous buttons.
    Diagnostics intentionally omit tracking URLs and query parameters.
    """
    items = []
    matches = {}
    diagnostics = []
    resolved = 0
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=BROWSER_ARGS)
            context = browser.new_context(locale='en-US', accept_downloads=False, user_agent=BROWSER_UA)
            page = context.new_page()
            try:
                _load_official_page(page)
                for frame_index, frame in enumerate(page.frames):
                    items.extend(_ticket_candidates_in_frame(frame, frame_index))
                matches, diagnostics = _match_official_ctas(events, items)
                print(f'Official ticket CTAs detected: {len(items)}')
                # Match item identity using the exact matched href and event reference.
                for event in events:
                    if event.get('ticket_url') or event['id'] not in matches:
                        continue
                    href = matches[event['id']]
                    event['ticket_tracking_url'] = href  # dashboard fallback if resolution fails
                    candidates = [item for item in items if _ticket_href(item) == href]
                    if len(candidates) != 1:
                        diagnostics.append({'result': 'duplicate_button', 'event_id': event['id']})
                        continue
                    item = candidates[0]
                    direct = resolve_ticket_url_http(href)
                    method = 'http' if direct else ''
                    if not direct and TRACKING_RE.match(href):
                        direct, method, _ = resolve_tracking_in_browser(context, href)
                    if not direct:
                        # Last resort: refresh and click the real button: previous click can navigate away.
                        _load_official_page(page)
                        fresh = []
                        for frame_index, frame in enumerate(page.frames):
                            fresh.extend(_ticket_candidates_in_frame(frame, frame_index))
                        # Ensure the same unique matched CTA exists in the refreshed DOM.
                        fresh_matches, _ = _match_official_ctas(events, fresh)
                        if fresh_matches.get(event['id']) == href:
                            buttons = [x for x in fresh if _ticket_href(x) == href]
                            if len(buttons) == 1:
                                button = buttons[0]
                                direct, method = _resolve_by_click(context, page, button['frame_index'], button['element_index'])
                            else:
                                method = 'button_ambiguous'
                        else:
                            method = 'button_changed'
                    if direct:
                        event['ticket_url'] = direct
                        event['ticket_provider'] = urlparse(direct).hostname or ''
                        resolved += 1
                        print(f"Ticket resolution: {event['id']} found {event['ticket_provider']} ({method})")
                    else:
                        print(f"Ticket resolution: {event['id']} unresolved ({method or 'http_no_redirect'})")
            finally:
                browser.close()
    except Exception as exc:
        print(f'Official ticket resolution unavailable ({type(exc).__name__})')
    print(f'Official CTA matches: {len(matches)}; direct URLs resolved: {resolved}')
    TICKET_DEBUG.parent.mkdir(parents=True, exist_ok=True)
    TICKET_DEBUG.write_text(json.dumps({'cta_count': len(items), 'matches': len(matches),
                                         'resolved': resolved, 'diagnostics': diagnostics[:100]}, indent=2) + '\n', encoding='utf-8')


def discover_event_ticket_urls(events):
    """Enrich every missing concert, including existing events, best effort.

    One Chromium session, limited per-page timeout; failures never block alerts.
    This is intentionally separate from Discord/Telegram and event identity.
    """
    missing = [e for e in events if not e.get('ticket_url') and EVENT_PAGE_RE.match(e.get('url', ''))]
    if not missing:
        return
    print(f'Ticket discovery: checking {len(missing)} event pages with missing direct links')
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=BROWSER_ARGS)
            context = browser.new_context(locale='en-US', user_agent=BROWSER_UA)
            page = context.new_page()
            for index, event in enumerate(missing, 1):
                try:
                    page.goto(event['url'], wait_until='domcontentloaded', timeout=14000)
                    page.wait_for_timeout(1200)
                    candidates = ticket_cta_candidates(page)
                    for candidate in candidates[:4]:
                        direct = resolve_ticket_url_http(candidate)
                        if direct:
                            event['ticket_url'] = direct
                            event['ticket_provider'] = urlparse(direct).hostname or ''
                            break
                    if not event.get('ticket_url') and candidates and TRACKING_RE.match(candidates[0]):
                        if not event.get('ticket_tracking_url'):
                            event['ticket_tracking_url'] = candidates[0]
                    print(f"Ticket discovery {index}/{len(missing)}: "
                          f"{'found ' + event['ticket_provider'] if event.get('ticket_url') else 'not available'} "
                          f"({event['id']})")
                except Exception as exc:
                    print(f"Ticket discovery {index}/{len(missing)} skipped ({type(exc).__name__})")
            browser.close()
    except Exception as exc:
        print(f'Ticket discovery unavailable ({type(exc).__name__}); preserving concert state')


def resolve_ticket_url_http(raw):
    """Resolve a dedicated CTA without navigating to arbitrary untrusted hosts.

    Only make an outbound request to Bandsintown tracking URLs. External
    direct HTTPS ticket URLs are validated, not fetched by the monitor.
    """
    direct = safe_ticket_url(raw)
    if direct:
        return direct
    if not TRACKING_RE.match(str(raw or '')):
        return ''
    try:
        # Redirect hops are checked before requests to avoid contacting private hosts.
        current = raw
        for _ in range(7):
            host = (urlparse(current).hostname or '').lower()
            if host != 'bandsintown.com' and not host.endswith('.bandsintown.com'):
                return safe_ticket_url(current)
            response = requests.get(current, timeout=8, allow_redirects=False,
                                    headers={'User-Agent': 'Mozilla/5.0'}, stream=True)
            location = response.headers.get('Location', '')
            status = response.status_code
            response.close()
            if status not in (301, 302, 303, 307, 308) or not location:
                break
            current = urljoin(current, location)
        return safe_ticket_url(current) if current != raw else ''
    except requests.RequestException:
        return ''


def enrich_tickets(events):
    """Enrich only event records, without changing event IDs or alert logic."""
    overrides = {}
    if TICKET_OVERRIDES.exists():
        try:
            overrides = json.loads(TICKET_OVERRIDES.read_text(encoding="utf-8"))
            if not isinstance(overrides, dict):
                overrides = {}
        except (ValueError, OSError):
            print("Warning: invalid ticket overrides JSON; ignoring it")
    for event in events:
        manual = safe_ticket_url(overrides.get(event["id"], ""))
        if not manual:
            for rule, destination in overrides.items():
                if "|" not in rule:
                    continue
                date, city = rule.split("|", 1)
                if event.get("date", "").startswith(date) and city.casefold() in (event.get("location", "") + " " + event.get("title", "")).casefold():
                    manual = safe_ticket_url(destination)
                    break
        candidate = event.get("ticket_url", "")
        if TRACKING_RE.match(candidate) and not event.get("ticket_tracking_url"):
            event["ticket_tracking_url"] = candidate
        # Do not visit a generic Bandsintown event-details page; it is not a ticket CTA.
        event["ticket_url"] = manual or resolve_ticket_url(candidate)
        if event["ticket_url"]:
            event["ticket_provider"] = urlparse(event["ticket_url"]).hostname or ""
        else:
            event["ticket_provider"] = ""
    discover_official_tickets(events)
    discover_event_ticket_urls(events)
    resolve_tracking_urls(events)
    found = sum(bool(e.get("ticket_url")) for e in events)
    print(f"Ticket discovery summary: {found}/{len(events)} events have direct ticket URLs")
    return events

# ---------------------------------------------------------------------------
# Flight cost ESTIMATE (heuristic, not a live fare). Home airport is configurable:
#   HOME_AIRPORT=WAW HOME_LAT=52.1657 HOME_LON=20.9671
# ---------------------------------------------------------------------------
HOME_AIRPORT = os.getenv("HOME_AIRPORT", "WAW")
HOME_COORDS = (float(os.getenv("HOME_LAT", "52.1657")), float(os.getenv("HOME_LON", "20.9671")))
GEOCODE_CACHE = Path("data/geocode_cache.json")


def _haversine_km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def _geocode(place, cache):
    """OpenStreetMap Nominatim lookup, cached on disk (1 request/second policy)."""
    key = place.casefold()
    if key in cache:
        return cache[key]
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": place, "format": "json", "limit": 1},
                         headers={"User-Agent": "BorisBrejchaConcertMonitor/1.0"}, timeout=15)
        data = r.json()
        coords = [float(data[0]["lat"]), float(data[0]["lon"])] if data else None
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return None  # network problem: do not cache, retry next run
    cache[key] = coords
    time.sleep(1.1)
    return coords


def estimate_flight(event, cache):
    """Rough economy return fare in EUR from HOME_AIRPORT, based on great-circle distance."""
    dates = sorted(_dates_in_text(event.get("date", "")))
    if not dates:
        return {}
    d1 = dates[0]
    if datetime.strptime(d1, "%Y-%m-%d") < datetime.now() - timedelta(days=1):
        return {}
    try:
        dest = (float(event.get("lat") or ""), float(event.get("lon") or ""))
    except ValueError:
        dest = None
    parts = [x.strip() for x in event.get("location", "").split(",") if x.strip()]
    place = event.get("city") or ", ".join(parts[-2:]) or event.get("location", "")
    if not dest:
        for query in (event.get("city"), event.get("location"), ", ".join(parts[-2:])):
            if query:
                dest = _geocode(query, cache)
                if dest:
                    break
    if not dest:
        return {}
    km = round(_haversine_km(HOME_COORDS, dest))
    d2 = (datetime.strptime(d1, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    out = {
        "flight_from": HOME_AIRPORT,
        "flight_km": km,
        "flight_search_url": "https://www.google.com/travel/flights?q=" + quote_plus(
            f"Flights from {HOME_AIRPORT} to {place} on {d1} through {d2}"),
    }
    if km < 250:
        out["flight_note"] = "Under ~250 km: train or car is probably better than flying"
    else:
        if km <= 4000:
            low, high = 40 + 0.05 * km, 80 + 0.12 * km
        else:
            low, high = 200 + 0.06 * km, 350 + 0.14 * km
        out["flight_est_eur_low"] = int(round(low / 5) * 5)
        out["flight_est_eur_high"] = int(round(high / 5) * 5)
    return out


def enrich_flights(events):
    """Best effort: never blocks alerts."""
    cache = {}
    if GEOCODE_CACHE.exists():
        try:
            cache = json.loads(GEOCODE_CACHE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            cache = {}
    before = len(cache)
    try:
        for event in events:
            event.update(estimate_flight(event, cache))
    except Exception as exc:
        print(f"Flight estimate unavailable ({type(exc).__name__})")
    if len(cache) != before:
        GEOCODE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        GEOCODE_CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def best_link(event):
    """Direct ticket seller > Bandsintown tracking link (redirects to seller) > event page."""
    return event.get("ticket_url") or event.get("ticket_tracking_url") or event.get("url") or URL


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
                city = lat = lon = ""

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

                    city = normalize(addr)
                    geo = loc.get("geo")
                    if isinstance(geo, dict):
                        lat, lon = geo.get("latitude", ""), geo.get("longitude", "")
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
                        "city": city, "lat": lat, "lon": lon,
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
                "ticket_url": next((urljoin(URL, a.get("href")) for a in el.select("a[href]") if re.search(r"ticket|buy|karten", a.get_text(" ", strip=True), re.I)), ""),
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
        f"🔗 {best_link(event)}"
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
        "🎧 New Boris Brejcha concert detected!\n"
        f"📅 Date: {event['date']}\n"
        f"🎵 Event: {event['title'][:180]}\n"
        f"📍 Location: {event['location'][:180] or 'See event page'}\n"
        f"🔗 {best_link(event)}"
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
    # Reuse previously resolved URLs to avoid repeated external requests each scan.
    old_events = {e.get("id"): e for e in (previous or {}).get("current_events", []) if isinstance(e, dict)}
    for event in events:
        old = old_events.get(event["id"], {})
        raw = event.get("ticket_url", "")
        if TRACKING_RE.match(raw):
            event["ticket_tracking_url"] = raw
        elif old.get("ticket_tracking_url"):
            event["ticket_tracking_url"] = old["ticket_tracking_url"]
        # Reuse a previously resolved seller URL (delete it from events.json to force a refresh).
        cached = safe_ticket_url(old.get("ticket_url", ""))
        if cached and (not raw or TRACKING_RE.match(raw)):
            event["ticket_url"] = cached
    enrich_tickets(events)
    enrich_flights(events)
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
