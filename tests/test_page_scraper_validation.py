"""Browser results must be validated before their text is used as research evidence."""
import unittest

from hearth.integrations import page_scraper


class PageValidationTests(unittest.TestCase):
    def test_http_error_status_is_unusable(self):
        for status in (401, 403, 404, 500):
            with self.subTest(status=status):
                self.assertTrue(page_scraper._page_is_unusable(status, "Site", "Some page body"))

    def test_access_denied_title_is_unusable(self):
        self.assertTrue(page_scraper._page_is_unusable(None, "403 Forbidden", ""))
        self.assertTrue(page_scraper._page_is_unusable(None, "", "Access Denied. Please sign in."))

    def test_not_found_text_is_unusable(self):
        self.assertTrue(page_scraper._page_is_unusable(200, "", "Page not found - try another search"))

    def test_reserved_and_bare_hosts_are_not_navigation_candidates(self):
        for url in ("https://duckduckgo.invalid/", "http://site.test/page",
                    "https://example.local/", "http://localhost:8000/"):
            with self.subTest(url=url):
                self.assertFalse(page_scraper.is_allowed_navigation_url(url))
        self.assertTrue(page_scraper.is_allowed_navigation_url("https://example.com/page"))

    def test_normal_page_is_usable(self):
        self.assertFalse(page_scraper._page_is_unusable(200, "Product guide", "A normal article about HTTP status codes."))


if __name__ == "__main__":
    unittest.main()
