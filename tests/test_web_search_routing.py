import unittest

import web_search_client as web_search


class WebSearchRoutingTests(unittest.TestCase):
    def test_weather_always_triggers_live_lookup(self):
        self.assertTrue(web_search.should_search_web("What will the weather be in Boston tomorrow?"))

    def test_weather_location_does_not_treat_contraction_as_city(self):
        self.assertIsNone(web_search.extract_weather_location("How's the weather?"))
        self.assertIsNone(web_search.extract_weather_location("How’s weather?"))

    def test_news_always_triggers_live_lookup(self):
        self.assertTrue(web_search.should_search_web("Give me the news about semiconductor exports"))

    def test_online_topic_research_triggers_live_lookup(self):
        self.assertTrue(web_search.should_search_web("Research solid-state battery progress online"))
        self.assertTrue(web_search.is_explicit_search_request("Research solid-state battery progress online"))

    def test_news_context_prefers_playwright_article_text(self):
        note = web_search.format_news_for_context("chip news", [{
            "title": "A headline",
            "url": "https://example.com/story",
            "date": "2026-09-04T10:00:00Z",
            "body": "short snippet",
            "full_text": "Detailed article text loaded in Playwright.",
        }])
        self.assertIn("Detailed article text loaded in Playwright.", note)
        self.assertNotIn("short snippet", note)


if __name__ == "__main__":
    unittest.main()
