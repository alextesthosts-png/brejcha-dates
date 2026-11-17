
"""Monitor Boris Brejcha tour dates; notify only about newly discovered events."""

import hashlib
import ipaddress
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.parse import parse_qs

import requests
from bs4 import BeautifulSoup
try:  # optional: Chrome TLS-fingerprint impersonation (pip install curl_cffi)
    from curl_cffi import requests as curl_requests
except Exception:
    curl_requests = None
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


BANDSINTOWN_EVENT_RE = re.compile(r"^https?://(?:[^/]+\.)?bandsintown\.com/(?:e|event)/(\d+)", re.I)


def legacy_key_for(event):
    """Original ID: hash of date|title|location (changes if any of them is edited)."""
    core = "|".join(
        normalize(event.get(k)).casefold()
        for k in ("date", "title", "location")
    )
    return hashlib.sha256(core.encode("utf-8")).hexdigest()[:20]


def key_for(event):
    """Stable ID: the Bandsintown event number when the URL has one, else the legacy hash.

    A Bandsintown ID survives start-time edits, venue renames and a switch between
    the JSON-LD and DOM extraction paths, which would otherwise look like new concerts.
    """
    match = BANDSINTOWN_EVENT_RE.match(normalize(event.get("url")))
    if match:
        return hashlib.sha256(f"bit:{match.group(1)}".encode("utf-8")).hexdigest()[:20]
    return legacy_key_for(event)


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
                popup.wait_for_url(lambda u: u.startswith('http') and not _is_bandsintown(u),
                                   timeout=8000, wait_until='commit')
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

# Hard limits so a blocked/slow ticket site can never stall the whole job.
TICKET_BUDGET_S = float(os.getenv("TICKET_BUDGET_S", "150"))   # total time for all ticket lookups
MAX_CONSECUTIVE_FAILS = 3                                      # then stop trying browser lookups
_BUDGET = {"deadline": None, "fails": 0, "t0": time.monotonic()}
_FAILED_LINKS = set()  # never retry a link that already failed in this run


def _budget_start():
    _BUDGET.update(deadline=time.monotonic() + TICKET_BUDGET_S, fails=0, t0=time.monotonic())


def _time_ok():
    return _BUDGET["deadline"] is None or time.monotonic() < _BUDGET["deadline"]


def _budget_ok():
    return _time_ok() and _BUDGET["fails"] < MAX_CONSECUTIVE_FAILS
# Interstitial pages sometimes redirect with JS or <meta refresh> instead of an HTTP 302.
JS_REDIRECT_RE = re.compile(
    r"""(?:location(?:\.href)?\s*=\s*|location\.(?:replace|assign)\(\s*|url=)["']?(https?:(?:\\?/){2}[^"'\s<>)]+)""",
    re.I)


def _is_bandsintown(url):
    host = (urlparse(url).hostname or "").lower()
    return host == "bandsintown.com" or host.endswith(".bandsintown.com")


_DIAG_DONE = [False]


def _launch(playwright):
    """Headless Chromium by default. HEADED_CHROME=1 -> real Chrome, not headless (run under xvfb)."""
    if os.getenv("HEADED_CHROME") == "1":
        for extra in ({"channel": "chrome"}, {}):
            try:
                return playwright.chromium.launch(headless=False, args=BROWSER_ARGS, **extra)
            except Exception as exc:
                print(f"Headed browser launch failed ({type(exc).__name__}); trying next option")
    return playwright.chromium.launch(headless=True, args=BROWSER_ARGS)


def _hdr_summary(r):
    h = r.headers
    keys = [k for k in ("server", "cf-ray", "cf-mitigated", "x-datadome", "x-amzn-waf-action", "content-type") if h.get(k)]
    out = f"status={r.status_code} " + " ".join(f"{k}={str(h.get(k))[:40]}" for k in keys)
    if h.get("Location"):
        out += f" location_host={urlparse(h.get('Location')).hostname}"
    return out.strip()


def resolve_tracking_with_curl(url):
    """Follow a /t/ link with a Chrome TLS fingerprint (curl_cffi). Only Bandsintown hops are fetched.
    Returns (final_url_or_empty, note)."""
    if curl_requests is None:
        return "", "curl_cffi_not_installed"
    current, note = url, ""
    try:
        for _ in range(8):
            r = curl_requests.get(current, impersonate="chrome", allow_redirects=False, timeout=6,
                                  headers={"Accept-Language": "en-US,en;q=0.9"})
            note = _hdr_summary(r)
            location = r.headers.get("Location", "")
            if r.status_code in (301, 302, 303, 307, 308) and location:
                current = urljoin(current, location)
                if not _is_bandsintown(current):
                    final = safe_ticket_url(current)
                    return (final, "curl_cffi") if final else ("", "unsafe_target")
                continue
            for m in JS_REDIRECT_RE.finditer((r.text or "")[:200000]):
                target = urljoin(current, m.group(1).replace("\\/", "/"))
                if not _is_bandsintown(target):
                    final = safe_ticket_url(target)
                    if final:
                        return final, "curl_cffi_page"
            break
    except Exception as exc:
        return "", f"curl_{type(exc).__name__}"
    return "", note


def _diagnose_once(url, curl_note):
    """One-time log line showing HOW Bandsintown answers this runner (status, WAF headers)."""
    if _DIAG_DONE[0]:
        return
    _DIAG_DONE[0] = True
    print(f"Diagnostic curl_cffi(chrome): {curl_note}")
    try:
        r = requests.get(url, timeout=6, allow_redirects=False, headers={"User-Agent": BROWSER_UA})
        print(f"Diagnostic requests: {_hdr_summary(r)}")
    except requests.RequestException as exc:
        print(f"Diagnostic requests: {type(exc).__name__}")


def resolve_tracking_in_browser(context, tracking_url, timeout_ms=8000):
    """Budgeted wrapper: 8 s per link, stops after 3 failures in a row or when time is up."""
    if tracking_url in _FAILED_LINKS:
        return "", "skipped_already_failed", []
    if not _budget_ok():
        return "", "skipped_budget", []
    if _BUDGET["deadline"] is not None:
        timeout_ms = int(min(timeout_ms, max(1000, (_BUDGET["deadline"] - time.monotonic()) * 1000)))
    final, curl_note = resolve_tracking_with_curl(tracking_url)
    if final:
        _BUDGET["fails"] = 0
        return final, curl_note, []
    _diagnose_once(tracking_url, curl_note)
    result = _resolve_tracking_in_browser(context, tracking_url, timeout_ms)
    _BUDGET["fails"] = 0 if result[0] else _BUDGET["fails"] + 1
    if not result[0]:
        _FAILED_LINKS.add(tracking_url)
    return result


def _resolve_tracking_in_browser(context, tracking_url, timeout_ms):
    """Open a Bandsintown /t/ link like a visitor and report where it ends up.

    Returns (direct_url, method, hosts_visited). Waits for the redirect to finish, which the
    old click code did not (it checked the URL after 1.8 s while still on bandsintown.com).
    """
    page = context.new_page()
    visited = []
    t0 = time.monotonic()
    page.on("framenavigated", lambda frame: visited.append(frame.url) if frame == page.main_frame else None)
    try:
        try:
            page.goto(tracking_url, wait_until="commit", timeout=timeout_ms)
        except Exception:
            pass  # aborted/slow navigations are fine; we inspect where we ended up
        try:
            page.wait_for_url(lambda u: u.startswith("http") and not _is_bandsintown(u),
                              timeout=max(500, timeout_ms - int((time.monotonic() - t0) * 1000)), wait_until="commit")
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
        html = ""
        if any(u.startswith("http") for u in visited):  # content() would block on a navigation that never committed
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
            browser = _launch(playwright)
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
            browser = _launch(playwright)
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
                    if not _time_ok():
                        print('Official ticket resolution: time budget used up, skipping the rest')
                        break
                    if event.get('ticket_url') or event['id'] not in matches:
                        continue
                    href = matches[event['id']]
                    event['ticket_tracking_url'] = href  # dashboard fallback if resolution fails
                    candidates = [item for item in items if _ticket_href(item) == href]
                    if len(candidates) != 1:
                        diagnostics.append({'result': 'duplicate_button', 'event_id': event['id']})
                        continue
                    direct = resolve_ticket_url_http(href)
                    method = 'http' if direct else ''
                    if not direct and TRACKING_RE.match(href):
                        direct, method, _ = resolve_tracking_in_browser(context, href)
                    if not direct and _budget_ok():
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
    if _BUDGET["fails"] >= MAX_CONSECUTIVE_FAILS:
        print("Ticket discovery: Bandsintown appears to block this runner; skipping event-page visits")
        return
    print(f'Ticket discovery: checking {len(missing)} event pages with missing direct links')
    try:
        with sync_playwright() as playwright:
            browser = _launch(playwright)
            context = browser.new_context(locale='en-US', user_agent=BROWSER_UA)
            page = context.new_page()
            for index, event in enumerate(missing, 1):
                if not _time_ok():
                    print('Ticket discovery: time budget used up, skipping the rest')
                    break
                try:
                    page.goto(event['url'], wait_until='domcontentloaded', timeout=10000)
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
    if not _time_ok():
        return ''
    try:
        # Redirect hops are checked before requests to avoid contacting private hosts.
        current = raw
        for _ in range(7):
            host = (urlparse(current).hostname or '').lower()
            if host != 'bandsintown.com' and not host.endswith('.bandsintown.com'):
                return safe_ticket_url(current)
            response = requests.get(current, timeout=5, allow_redirects=False,
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
    _budget_start()
    overrides = {}
    if TICKET_OVERRIDES.exists():
        try:
            overrides = json.loads(TICKET_OVERRIDES.read_text(encoding="utf-8"))
            if not isinstance(overrides, dict):
                overrides = {}
        except (ValueError, OSError):
            print("Warning: invalid ticket overrides JSON; ignoring it")
    for event in events:
        manual = safe_ticket_url(overrides.get(event["id"]) or overrides.get(event.get("legacy_id"), ""))
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
        event["ticket_url"] = manual or resolve_ticket_url_http(candidate)
        if event["ticket_url"]:
            event["ticket_provider"] = urlparse(event["ticket_url"]).hostname or ""
        else:
            event["ticket_provider"] = ""
    discover_official_tickets(events)
    discover_event_ticket_urls(events)
    resolve_tracking_urls(events)
    found = sum(bool(e.get("ticket_url")) for e in events)
    print(f"Ticket discovery summary: {found}/{len(events)} events have direct ticket URLs "
          f"({time.monotonic() - _BUDGET['t0']:.0f}s, browser failures in a row: {_BUDGET['fails']})")
    return events

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
        event["legacy_id"] = legacy_key_for(event)
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


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# More new events than this in one run is treated as suspicious (e.g. the site changed
# its markup and every event got a new ID): send one summary instead of an alert flood.
MAX_INDIVIDUAL_ALERTS = _env_int("MAX_INDIVIDUAL_ALERTS", 5)


def format_alert(event, markdown=False):
    b = "**" if markdown else ""
    return (
        f"🎧 {b}New Boris Brejcha concert detected!{b}\n"
        f"📅 {b}Date:{b} {event['date']}\n"
        f"🎵 {b}Event:{b} {event['title'][:180]}\n"
        f"📍 {b}Location:{b} {event['location'][:180] or 'See event page'}\n"
        f"🔗 {best_link(event)}"
    )


def format_bulk_alert(events, markdown=False):
    b = "**" if markdown else ""
    lines = [
        f"⚠️ {b}{len(events)} Boris Brejcha concerts appeared at once.{b}",
        "Either a large tour was announced or the dates page changed its layout. "
        "They were recorded without individual alerts:",
    ]
    for event in events[:12]:
        where = f" ({event['location'][:80]})" if event.get("location") else ""
        lines.append(f"• {event['date'][:10]} — {event['title'][:100]}{where}")
    if len(events) > 12:
        lines.append(f"…and {len(events) - 12} more.")
    lines.append(f"🔗 {URL}")
    return "\n".join(lines)


def post_discord(content, webhook):
    """POST a message to a Discord webhook. Never let the webhook URL reach logs."""
    payload = {
        "content": content[:1900],
        "allowed_mentions": {"parse": []},
    }
    headers = {"User-Agent": "BorisBrejchaConcertMonitor/1.0"}

    for attempt in range(3):
        try:
            response = requests.post(webhook, json=payload, headers=headers, timeout=30)
        except requests.RequestException:
            # The exception text contains the webhook path (the secret), so drop it.
            raise RuntimeError("Discord request failed (network or timeout).") from None

        if response.status_code == 429 and attempt < 2:
            try:
                delay = float(response.json().get("retry_after", 2))
            except (ValueError, TypeError, AttributeError):
                delay = 2
            time.sleep(max(0, min(delay, 60)))
            continue

        if not response.ok:
            raise RuntimeError(f"Discord rejected notification (HTTP {response.status_code}).")

        print("Discord notification delivered successfully.")
        return


def post_telegram(text, bot_token, chat_id):
    """Send a message with the Telegram Bot API. Never let the token reach logs."""
    # Telegram's Bot API returns HTTP 200 with {"ok": false} in some errors.
    for attempt in range(3):
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text[:4000],
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
            raise RuntimeError(f"Telegram rejected notification (HTTP {response.status_code}).")
        print("Telegram notification delivered successfully.")
        return


def send_discord(event, webhook):
    """Send one real or sample concert notification."""
    post_discord(format_alert(event, markdown=True), webhook)


def send_telegram(event, bot_token, chat_id):
    """Send a concert notification using the Telegram Bot API."""
    post_telegram(format_alert(event), bot_token, chat_id)


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


def reuse_cached_tickets(events, previous):
    """Reuse previously resolved URLs to avoid repeated external requests each scan."""
    old_events = {}
    for old in (previous or {}).get("current_events", []):
        if isinstance(old, dict):
            for key in (old.get("legacy_id"), old.get("id")):
                if key:
                    old_events[key] = old
    for event in events:
        old = old_events.get(event["id"]) or old_events.get(event.get("legacy_id"), {})
        raw = event.get("ticket_url", "")
        if TRACKING_RE.match(raw):
            event["ticket_tracking_url"] = raw
        elif old.get("ticket_tracking_url"):
            event["ticket_tracking_url"] = old["ticket_tracking_url"]
        # Reuse a previously resolved seller URL (delete it from events.json to force a refresh).
        cached = safe_ticket_url(old.get("ticket_url", ""))
        if cached and (not raw or TRACKING_RE.match(raw)):
            event["ticket_url"] = cached


def queue_new_events(events, known, pending, first_seen, now):
    """Migrate legacy IDs, then queue alerts for events that are neither known nor queued.

    Returns the list of newly queued events. Mutates known, pending and first_seen.
    """
    # Events recorded under the old date|title|location ID keep their history.
    for event in events:
        legacy = event.get("legacy_id")
        if event["id"] not in known and legacy in known:
            known.add(event["id"])
            if legacy in first_seen:
                first_seen.setdefault(event["id"], first_seen[legacy])

    queued = set(pending)
    for item in pending.values():
        queued.update(item.get("event_ids", []))

    new_events = [
        e for e in events
        if e["id"] not in known and e["id"] not in queued and e.get("legacy_id") not in queued
    ]

    if len(new_events) > MAX_INDIVIDUAL_ALERTS:
        print(f"WARNING: {len(new_events)} new events at once (limit {MAX_INDIVIDUAL_ALERTS}); "
              "sending one summary instead of individual alerts.")
        pending[f"bulk:{now}"] = {
            "bulk": [{k: e.get(k, "") for k in ("date", "title", "location")} for e in new_events],
            "event_ids": [e["id"] for e in new_events],
            "delivered": [],
        }
    else:
        for event in new_events:
            pending.setdefault(event["id"], {"event": event, "delivered": []})

    for event in new_events:
        first_seen.setdefault(event["id"], now)
    for key in pending:
        if not key.startswith("bulk:"):
            first_seen.setdefault(key, now)
    return new_events


def deliver_pending(known, events, pending, first_seen, webhook, bot_token, chat_id):
    """Send every queued alert to both destinations, saving progress after each step."""
    for key in list(pending):
        item = pending[key]
        delivered = set(item.get("delivered", []))
        if "bulk" in item:
            discord_text = format_bulk_alert(item["bulk"], markdown=True)
            telegram_text = format_bulk_alert(item["bulk"])
        else:
            discord_text = format_alert(item["event"], markdown=True)
            telegram_text = format_alert(item["event"])

        if "discord" not in delivered:
            post_discord(discord_text, webhook)
            delivered.add("discord")
            item["delivered"] = sorted(delivered)
            save_state(known, events, pending, first_seen)

        if "telegram" not in delivered:
            post_telegram(telegram_text, bot_token, chat_id)
            delivered.add("telegram")
            item["delivered"] = sorted(delivered)
            save_state(known, events, pending, first_seen)

        # Mark fully processed only after both destinations accept the alert.
        if "bulk" in item:
            known.update(item.get("event_ids", []))
        else:
            known.add(key)
        del pending[key]
        save_state(known, events, pending, first_seen)


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
    reuse_cached_tickets(events, previous)

    if previous is None:
        enrich_tickets(events)
        print(f"Initial baseline: {len(events)} concerts recorded, no notifications sent.")
        save_state({event["id"] for event in events}, events, first_seen={})
        return

    known = set(previous.get("known_ids", []))
    pending = previous.get("pending_notifications", {})
    if not isinstance(pending, dict):
        raise RuntimeError("Invalid pending_notifications in state file.")
    first_seen = previous.get("first_seen", {})
    if not isinstance(first_seen, dict):
        raise RuntimeError("Invalid first_seen in state file.")

    new_events = queue_new_events(
        events, known, pending, first_seen, datetime.now(timezone.utc).isoformat()
    )
    print(f"{len(events)} currently found; {len(new_events)} newly queued; "
          f"{len(pending)} alert(s) pending.")

    # Persist the queue BEFORE the slow ticket lookups, so a timeout cannot lose an alert.
    save_state(known, events, pending, first_seen)

    enrich_tickets(events)
    # Give queued alerts the ticket links found during enrichment.
    by_id = {event["id"]: event for event in events}
    for key, item in pending.items():
        if "event" in item and key in by_id:
            item["event"] = by_id[key]
    save_state(known, events, pending, first_seen)

    deliver_pending(known, events, pending, first_seen, webhook, bot_token, chat_id)
    save_state(known, events, pending, first_seen)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(line_buffering=True)  # show logs live in GitHub Actions
    except Exception:
        pass
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
