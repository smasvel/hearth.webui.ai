"""Playwright helper for browsing Hugging Face models and downloading files.

Usage:
    python tools/huggingface_playwright_download.py --query "text-generation" --limit 3 --download config.json

Notes:
    - Requires playwright browsers: python -m playwright install chromium
    - Downloads go to the directory passed via --output (default: ./hf_downloads)
    - Public models only unless HF_TOKEN is set in the environment.
"""

import argparse
import os
import re
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description="Browse and download Hugging Face models with Playwright")
    parser.add_argument("--query", default="text-generation", help="Search query on Hugging Face models page")
    parser.add_argument("--limit", type=int, default=3, help="Maximum number of models to visit")
    parser.add_argument("--download", default="config.json", help="Filename to download from each model page")
    parser.add_argument("--output", default="hf_downloads", help="Directory for downloaded files")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        search_url = f"https://huggingface.co/models?search={args.query}"
        page.goto(search_url, wait_until="networkidle")

        # Collect model links from the search results page.
        links = page.locator("a[href^='/'][href*='/'][href$='']").all()
        model_urls = []
        for link in links:
            href = link.get_attribute("href") or ""
            # Model repos look like /org/model or /user/model.
            if re.fullmatch(r"/[^/]+/[^/]+", href):
                full = "https://huggingface.co" + href
                if full not in model_urls:
                    model_urls.append(full)
            if len(model_urls) >= args.limit:
                break

        for model_url in model_urls:
            print(f"Visiting {model_url}")
            page.goto(model_url, wait_until="networkidle")

            # Try to download the requested file from the "Files and versions" tab.
            files_tab = page.get_by_role("link", name=re.compile("Files", re.I))
            if files_tab.count():
                files_tab.first.click()
                page.wait_for_load_state("networkidle")

            file_link = page.get_by_role("link", name=re.compile(re.escape(args.download), re.I))
            if file_link.count():
                with page.expect_download() as download_info:
                    file_link.first.click()
                download = download_info.value
                target = output_dir / f"{model_url.rstrip('/').split('/')[-1]}_{download.suggested_filename}"
                download.save_as(str(target))
                print(f"Downloaded {download.suggested_filename} to {target}")
            else:
                print(f"No file named {args.download} found for {model_url}")

        browser.close()


if __name__ == "__main__":
    main()
