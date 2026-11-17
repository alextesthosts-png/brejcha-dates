import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

import monitor
from monitor import key_for, legacy_key_for, parse_events


def event(n, url=None, **extra):
    e = {
        "date": f"2027-05-{n:02d}T21:00:00",
        "title": f"Boris Brejcha @ Venue {n}",
        "location": f"Venue {n} City, Country",
        "url": url or f"https://www.bandsintown.com/e/10000{n}",
    }
    e.update(extra)
    e["id"] = key_for(e)
    e["legacy_id"] = legacy_key_for(e)
    return e


class ExtractionTests(unittest.TestCase):
    def test_jsonld(self):
        html = '''<script type="application/ld+json">{"@context":"https://schema.org","@type":"MusicEvent","name":"Boris Brejcha Live","startDate":"2027-05-15","location":{"@type":"Place","name":"Example Arena","address":{"addressLocality":"Berlin","addressCountry":"DE"}},"url":"https://example.org/show"}</script>'''
        result = parse_events(html)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['date'], '2027-05-15')
        self.assertIn('Berlin', result[0]['location'])

    def test_dom(self):
        html = '<div class="tour-date"><time datetime="2027-05-15">15 May</time><h3>Boris Brejcha</h3><span class="venue">Example Arena</span><a href="/dates">Details</a></div>'
        result = parse_events(html)
        self.assertEqual(len(result), 1)

    def test_stable_key(self):
        event = {'date': '2027-05-15', 'title': 'Boris Brejcha', 'location': 'Berlin'}
        self.assertEqual(key_for(event), key_for(dict(event, url='https://example.org/other')))

    def test_bandsintown_key_survives_edits(self):
        a = {'date': '2027-05-15T21:00:00', 'title': 'Boris Brejcha @ Arena',
             'location': 'Arena Berlin', 'url': 'https://www.bandsintown.com/e/108680336'}
        b = dict(a, date='2027-05-15T22:00:00', title='Boris Brejcha @ New Arena Name')
        self.assertEqual(key_for(a), key_for(b))
        self.assertNotEqual(legacy_key_for(a), legacy_key_for(b))


class TicketUrlTests(unittest.TestCase):
    def test_safe_ticket_url(self):
        ok = monitor.safe_ticket_url
        self.assertEqual(ok('https://tickets.example.com/e/1#x'), 'https://tickets.example.com/e/1')
        for bad in ('http://tickets.example.com/', 'https://localhost/x', 'https://10.0.0.1/x',
                    'https://user:pw@example.com/', 'https://www.bandsintown.com/t/1',
                    'https://printer.local/', 'javascript:alert(1)', ''):
            self.assertEqual(ok(bad), '', bad)

    def test_cta_matching_by_bandsintown_id_and_ambiguity(self):
        e1, e2 = event(1), event(2)
        items = [
            {'href': 'https://www.bandsintown.com/t/100001?came_from=1', 'anchors': [], 'context': ''},
            {'href': 'https://www.bandsintown.com/t/999?x=1', 'anchors': [], 'context': 'nothing useful'},
        ]
        matches, diagnostics = monitor._match_official_ctas([e1, e2], items)
        self.assertEqual(matches, {e1['id']: items[0]['href']})
        self.assertEqual(diagnostics[1]['result'], 'unmatched')


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patch = mock.patch.object(monitor, 'STATE', Path(self.tmp.name) / 'events.json')
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.discord = mock.patch.object(monitor, 'post_discord').start()
        self.telegram = mock.patch.object(monitor, 'post_telegram').start()
        self.addCleanup(mock.patch.stopall)

    def state(self):
        return json.loads(monitor.STATE.read_text(encoding='utf-8'))

    def run_once(self, known, events, pending=None, first_seen=None):
        pending = {} if pending is None else pending
        first_seen = {} if first_seen is None else first_seen
        monitor.queue_new_events(events, known, pending, first_seen, '2026-11-17T00:00:00+00:00')
        monitor.deliver_pending(known, events, pending, first_seen, 'hook', 'tok', 'chat')
        return known, pending, first_seen

    def test_new_event_sent_to_both_then_known(self):
        e = event(1)
        known, pending, first_seen = self.run_once(set(), [e])
        self.assertEqual(self.discord.call_count, 1)
        self.assertEqual(self.telegram.call_count, 1)
        self.assertIn(e['id'], known)
        self.assertEqual(pending, {})
        self.assertIn(e['id'], first_seen)

    def test_telegram_failure_retries_only_telegram(self):
        e = event(1)
        self.telegram.side_effect = RuntimeError('down')
        known, pending = set(), {}
        with self.assertRaises(RuntimeError):
            self.run_once(known, [e], pending)
        saved = self.state()
        self.assertEqual(saved['pending_notifications'][e['id']]['delivered'], ['discord'])
        self.assertNotIn(e['id'], saved['known_ids'])

        # Next run: event vanished from the page, still delivered to Telegram only.
        self.telegram.side_effect = None
        self.discord.reset_mock()
        self.telegram.reset_mock()
        known = set(saved['known_ids'])
        pending = saved['pending_notifications']
        self.run_once(known, [], pending, saved['first_seen'])
        self.discord.assert_not_called()
        self.assertEqual(self.telegram.call_count, 1)
        self.assertIn(e['id'], known)

    def test_legacy_ids_are_migrated_without_alerts(self):
        events = [event(n) for n in range(1, 10)]
        known = {e['legacy_id'] for e in events}
        first_seen = {events[0]['legacy_id']: '2026-01-01T00:00:00+00:00'}
        known, pending, first_seen = self.run_once(known, events, first_seen=first_seen)
        self.discord.assert_not_called()
        self.assertTrue(all(e['id'] in known for e in events))
        self.assertEqual(first_seen[events[0]['id']], '2026-01-01T00:00:00+00:00')

    def test_flood_becomes_one_summary(self):
        events = [event(n) for n in range(1, monitor.MAX_INDIVIDUAL_ALERTS + 3)]
        known, pending, _ = self.run_once(set(), events)
        self.assertEqual(self.discord.call_count, 1)
        self.assertEqual(self.telegram.call_count, 1)
        self.assertIn('appeared at once', self.discord.call_args[0][0])
        self.assertTrue(all(e['id'] in known for e in events))

    def test_queued_bulk_is_not_requeued(self):
        events = [event(n) for n in range(1, monitor.MAX_INDIVIDUAL_ALERTS + 3)]
        known, pending, first_seen = set(), {}, {}
        monitor.queue_new_events(events, known, pending, first_seen, 'T1')
        monitor.queue_new_events(events, known, pending, first_seen, 'T2')
        self.assertEqual(len(pending), 1)


class SecretHandlingTests(unittest.TestCase):
    def test_discord_network_error_does_not_leak_webhook(self):
        secret = 'https://discord.com/api/webhooks/123/SECRET-TOKEN'
        err = requests.ConnectionError('Max retries exceeded with url: /api/webhooks/123/SECRET-TOKEN')
        with mock.patch.object(monitor.requests, 'post', side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                monitor.post_discord('hi', secret)
        self.assertNotIn('SECRET-TOKEN', str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertTrue(ctx.exception.__suppress_context__)


if __name__ == '__main__':
    unittest.main()
