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
    payload = {'content': content[:1900], 'allowed_mentions': {'parse': []}}
    headers = {'User-Agent': 'BorisBrejchaConcertMonitor/1.0',
               'Content-Type': 'application/json'}
    for attempt in range(3):
        response = requests.post(webhook, json=payload, headers=headers, timeout=30)
        if response.status_code == 429 and attempt < 2:
            try:
                delay = float(response.json().get('retry_after', 2))
            except (ValueError, TypeError, AttributeError):
                delay = 2
            time.sleep(max(0, min(delay, 60)))
            continue
        if not response.ok:
            # Never print response.url: it contains the secret webhook token.
            print(f'Discord notification failed: HTTP {response.status_code}; '
                  f'response: {response.text[:300]}', file=sys.stderr)
        response.raise_for_status()
        return


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

