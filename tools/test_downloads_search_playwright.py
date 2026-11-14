"""Playwright smoke test for Hearth Downloads search results.

Usage:
    HEARTH_URL=https://localhost:8443 python tools/test_downloads_search_playwright.py
"""

import os
from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("HEARTH_URL", "https://localhost:5443")
USERNAME = os.environ.get("HEARTH_USER")
PASSWORD = os.environ.get("HEARTH_PASSWORD")
if not PASSWORD:
    creds_path = os.path.join(os.path.dirname(__file__), "..", "test-user")
    if os.path.isfile(creds_path):
        lines = [line.strip() for line in open(creds_path) if line.strip()]
        if len(lines) >= 2:
            USERNAME = USERNAME or lines[0]
            PASSWORD = lines[1]
if not USERNAME or not PASSWORD:
    raise SystemExit("Set HEARTH_USER and HEARTH_PASSWORD (or create robotest-credentials).")


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        public = browser.new_page()
        public.goto("https://lemonade-server.ai/models.html", wait_until="domcontentloaded")
        public.wait_for_timeout(3000)
        lemonade_public = public.locator("body").inner_text()
        public.goto("https://lmstudio.ai/models?filter=tools", wait_until="domcontentloaded")
        lm_public = public.locator("body").inner_text()
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

        # Compare identifiable public catalog examples with Hearth's rendered search.
        for backend, query, public_text, expected in (
            ("lemonade", "nemotron", lemonade_public, "NVIDIA-Nemotron-3.5-Lightning"),
            ("lemonade", "Muse Glimmer", lemonade_public, "unsloth/Muse-Glimmer-30B-GGUF"),
            ("lmstudio", "lfm3", lm_public, "lmstudio-community/LFM3-VL-2B-GGUF"),
            ("lmstudio", "flint", lm_public, "lmstudio-community/flint-GGUF"),
        ):
            data = page.evaluate("""async ({backend, query}) => {
                const response = await fetch(`/api/models/search?backend=${backend}&q=${encodeURIComponent(query)}`);
                return await response.json();
            }""", {"backend": backend, "query": query})
            names = [item.get("catalog_name") or item.get("id") for item in data.get("results", [])]
            print(f"{backend} {query!r}: public={'yes' if query.split()[0].lower() in public_text.lower() else 'no'}, Hearth={len(names)} results, examples={names[:5]}")
            assert any(expected.casefold() in str(item.get("id", "")).casefold() or
                       expected.casefold() in str(item.get("catalog_name", "")).casefold()
                       for item in data.get("results", [])), f"Hearth missed {expected}"

        page.fill("#catalog-search-input", "Muse Glimmer")
        page.click("#catalog-search-btn")
        page.locator("#catalog-results .model-name", has_text="Muse-Glimmer-30B-GGUF").first.wait_for(timeout=30000)
        page.fill("#lmstudio-search-input", "lfm3")
        page.click("#lmstudio-search-btn")
        page.locator("#lmstudio-results .model-name", has_text="LFM3-VL-2B-GGUF").first.wait_for(timeout=30000)

        browser.close()


if __name__ == "__main__":
    main()
