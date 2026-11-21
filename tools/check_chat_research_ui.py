"""Check browser-reported research progress in the signed-in Chat page."""
import contextlib
import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


release_browser = threading.Event()


def read_pages(urls, **kwargs):
    callback = app.page_scraper.playwright_progress.get()
    assert callback
    callback("Playwright is reading page 1 of 1…")
    if not release_browser.wait(10):
        raise RuntimeError("The browser progress was never observed.")
    callback("Playwright read 1 of 1 pages.")
    return {urls[0]: {"text": "This is the public research fixture.", "html": ""}}


def answer(*args, **kwargs):
    yield "data: " + json.dumps({"choices": [{"delta": {"content": "Based on the public research fixture."}}]})
    yield "data: [DONE]"


with contextlib.ExitStack() as stack:
    user = app.users_store.create_user("research-browser-test", "test-password")
    stack.enter_context(patch.dict(app.app.config, {"SESSION_COOKIE_SECURE": False}))
    stack.enter_context(patch.object(app.page_scraper, "available", return_value=True))
    stack.enter_context(patch.object(app.page_scraper, "fetch_pages", side_effect=read_pages))
    stack.enter_context(patch.object(app.lms, "chat_stream", side_effect=answer))
    stack.enter_context(patch.object(app, "_recall_memory", return_value=None))
    stack.enter_context(patch.object(app, "_remember_exchange"))
    for backend in (app.lemonade, app.kobold, app.vllm, app.q38rocm, app.fastflowlm, app.zen):
        stack.enter_context(patch.object(backend, "owns_model", return_value=False))
    server = make_server("127.0.0.1", 0, app.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stack.callback(server.shutdown)
    stack.callback(release_browser.set)

    client = app.app.test_client()
    with client.session_transaction() as session:
        session.update(logged_in=True, user_id=user["id"], last_activity=time.time(), login_csrf_token="fixture")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page()
        cookie = app.app.config["SESSION_COOKIE_NAME"]
        page.context.add_cookies([{"name": cookie, "value": client.get_cookie(cookie).value,
                                    "domain": "127.0.0.1", "path": "/"}])
        page.route("**/api/models", lambda route: route.fulfill(json={"models": [{
            "id": "research-fixture", "backend": "lmstudio", "type": "llm", "state": "loaded",
        }]}))
        page.goto(f"http://127.0.0.1:{server.server_port}/")
        page.locator("#model-select").select_option("research-fixture")
        page.locator("#chat-input").fill("Use Playwright to read https://example.org/")
        page.locator("#send-btn").click()
        progress = page.locator("#chat-research-status")
        expect(progress).to_have_text("Playwright is reading page 1 of 1…")
        expect(page.locator("#send-btn")).to_have_text("Stop")
        release_browser.set()
        expect(page.get_by_text("Based on the public research fixture.", exact=True)).to_be_visible()
        expect(progress).to_have_text("Playwright read 1 of 1 pages.")
        page.locator("#new-chat").click()
        expect(progress).to_be_hidden()
        browser.close()
        print("PASS Chat shows Playwright progress, completion, and clears it on conversation change.")
