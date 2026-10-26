import unittest
from monitor import parse_events, key_for

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
        event = {'date':'2027-05-15','title':'Boris Brejcha','location':'Berlin'}
        self.assertEqual(key_for(event), key_for(dict(event, url='https://example.org/other')))

if __name__ == '__main__':
    unittest.main()
