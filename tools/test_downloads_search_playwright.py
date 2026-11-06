"""Playwright smoke test for Hearth Downloads search results.

Usage:
    HEARTH_URL=https://localhost:8443 python tools/test_downloads_search_playwright.py
"""

import os
from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("HEARTH_URL", "https://localhost:8443")
USERNAME = os.environ.get("HEARTH_USER")
PASSWORD = os.environ.get("HEARTH_PASSWORD")
if not USERNAME or not PASSWORD:
    raise SystemExit("Set HEARTH_USER and HEARTH_PASSWORD before running this test.")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(ignore_https_errors=True)
        page.goto(BASE_URL + "/login", wait_until="networkidle")
        page.fill('input[name="username"]', USERNAME)
        page.fill('input[name="password"]', PASSWORD)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")

        page.goto(BASE_URL + "/downloads", wait_until="networkidle")

        # Lemonade/catalog search
        page.fill("#catalog-search-input", "qwen")
        page.click("#catalog-search-btn")
        page.wait_for_timeout(5000)
        lemonade_cards = page.locator("#catalog-results .result-card, #catalog-results .download-card, #catalog-results .model-card").count()
        print("Lemonade/catalog result cards:", lemonade_cards)

        # LM Studio search
        page.fill("#lmstudio-search-input", "llama")
        page.click("#lmstudio-search-btn")
        page.wait_for_timeout(5000)
        lm_cards = page.locator("#lmstudio-results .result-card, #lmstudio-results .download-card, #lmstudio-results .model-card").count()
        print("LM Studio result cards:", lm_cards)

        browser.close()


if __name__ == "__main__":
    main()
