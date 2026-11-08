"""Broad Playwright smoke test for Hearth pages/tabs."""

import os
from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("HEARTH_URL", "https://localhost:5443")


def creds():
    user = os.environ.get("HEARTH_USER", "robotest")
    pw = os.environ.get("HEARTH_PASSWORD")
    if not pw:
        p = os.path.join(os.path.dirname(__file__), "..", "robotest-credentials")
        if os.path.isfile(p):
            lines = [x.strip() for x in open(p) if x.strip()]
            if len(lines) >= 2:
                return lines[0], lines[1]
    return user, pw


def main():
    user, pw = creds()
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(ignore_https_errors=True)
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("response", lambda r: errors.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)

        page.goto(BASE_URL + "/login", wait_until="networkidle")
        page.fill('input[name="username"]', user)
        page.fill('input[name="password"]', pw)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")

        paths = [
            "/", "/chat", "/models", "/downloads", "/metrics", "/agents", "/quizzes",
            "/settings", "/help", "/browse-history", "/model-tests",
        ]
        for path in paths:
            try:
                page.goto(BASE_URL + path, wait_until="networkidle", timeout=15000)
                print(f"OK {path} -> {page.url}")
            except Exception as e:
                print(f"FAIL {path}: {e}")
                errors.append(str(e))

        browser.close()

    if errors:
        print("\nConsole/page errors:")
        for e in errors[:20]:
            print(" -", e)
    else:
        print("\nNo console errors detected.")


if __name__ == "__main__":
    main()
