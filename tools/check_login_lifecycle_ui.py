"""Exercise sign-in, live chat recovery, and sign-out in a real browser."""
import contextlib
import json
import os
import re
import threading

from playwright.sync_api import expect, sync_playwright
from unittest.mock import patch
from werkzeug.serving import make_server


def main():
    if not os.environ.get("HEARTH_DATABASE_SCHEMA", "").startswith("hearth_test_"):
        raise SystemExit("Run this suite with tools/run_browser_test.py.")
    import app

    with contextlib.ExitStack() as stack:
        app.users_store.create_user("browser-admin-fixture", "fixture-password", "admin")
        app.users_store.create_user("browser-chat-fixture", "fixture-password", "user")
        stack.enter_context(patch.dict(app.app.config, {"SESSION_COOKIE_SECURE": False}))
        stack.enter_context(patch.object(app.help_data, "build", return_value={"ports": {}, "ca_trust_commands": {}}))
        server = make_server("127.0.0.1", 0, app.app, threaded=True)
        stack.callback(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        stack.callback(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"

        playwright = stack.enter_context(sync_playwright())
        browser = playwright.chromium.launch(headless=True)
        stack.callback(browser.close)
        page = browser.new_page()
        for route in ("/", "/models", "/downloads", "/live-logs", "/agents/skills"):
            page.goto(base + route)
            expect(page).to_have_url(re.compile(r"/login(?:\?|$)"))
            expect(page.get_by_role("button", name="Sign in securely")).to_be_visible()
            print(f"CASE PASS signed-out visitor redirected from {route}")

        page.goto(base + "/login")
        page.locator('[name="username"]').fill("browser-chat-fixture")
        page.locator('[name="password"]').fill("wrong-fixture-password")
        page.get_by_role("button", name="Sign in securely").click()
        expect(page.get_by_text("Invalid username or password")).to_be_visible()
        print("CASE PASS invalid password displays sign-in error")

        page.locator('[name="username"]').fill("browser-chat-fixture")
        page.locator('[name="password"]').fill("fixture-password")
        page.get_by_role("button", name="Sign in securely").click()
        expect(page).to_have_url(base + "/")
        expect(page.locator("#model-select")).to_be_visible()
        print("CASE PASS valid sign-in opens chat after invalid attempt")

        finish_reply = threading.Event()

        def reply_stream(*_args, **_kwargs):
            for content in ("Browser fixture started.", " Browser fixture finished."):
                yield "data: " + json.dumps({"choices": [{"delta": {"content": content}}]})
                if content == "Browser fixture started.":
                    finish_reply.wait(timeout=20)
            yield "data: [DONE]"

        stack.callback(finish_reply.set)
        for backend in (app.lemonade, app.q38rocm, app.vllm, app.zen, app.kobold, app.fastflowlm):
            stack.enter_context(patch.object(backend, "owns_model", return_value=False))
        stack.enter_context(patch.object(app, "_recall_memory", return_value=None))
        stack.enter_context(patch.object(app, "_remember_exchange"))
        stack.enter_context(patch.object(app.lms, "chat_stream", side_effect=reply_stream))
        page.route("**/api/models", lambda route: route.fulfill(json={"models": [
            {"id": "browser-model", "backend": "lmstudio", "type": "llm", "state": "loaded"},
        ]}))
        page.reload()
        expect(page.locator("#model-select")).to_have_value("browser-model")
        page.locator("#chat-input").fill("Reply with a greeting.")
        page.locator("#send-btn").click()
        expect(page.get_by_text("Browser fixture started.", exact=True)).to_be_visible()
        print("CASE PASS chat renders partial model output before completion")

        page.reload()
        expect(page.get_by_text("Browser fixture started.", exact=True)).to_be_visible()
        print("CASE PASS chat reconnects to a running reply after reload")

        finish_reply.set()
        reply = page.get_by_text("Browser fixture started. Browser fixture finished.", exact=True)
        expect(reply).to_be_visible()
        page.reload()
        expect(reply).to_have_count(1)
        expect(reply).to_be_visible()
        print("CASE PASS completed reply persists and appears once after reload")

        chart_reply = (
            "Browser chart fixture\n\n```svg\n"
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 60">'
            '<polyline points="0,50 50,30 100,10" fill="none" stroke="blue"/>'
            "</svg>\n```"
        )
        app.settings_store.set("web_search_enabled", "true")
        with patch.object(app.historical_chart, "fetch_daily_chart", return_value=chart_reply) as chart_source:
            page.set_viewport_size({"width": 390, "height": 844})
            page.locator("#chat-input").fill("Show 4y 1D ABCD chart here")
            page.locator("#send-btn").click()
            chart = page.get_by_role("img", name="Daily closing-price chart")
            expect(chart).to_be_visible()
            expect(chart).to_be_in_viewport()
            page.wait_for_function("document.querySelector('.chat-chart-image')?.naturalWidth > 0")
            print("CASE PASS chart decodes and displays in mobile chat without reload")

            with page.expect_popup() as full_size:
                page.get_by_role("link", name="Open chart full size").click()
            expect(full_size.value.locator("svg polyline")).to_be_visible()
            full_size.value.close()
            page.reload()
            expect(chart).to_be_visible()
            print("CASE PASS chart opens full size and persists after refresh")

            page.locator("#chat-input").fill("Show 2y 1 D chart here")
            page.locator("#send-btn").click()
            expect(chart).to_have_count(2)
            chart_source.assert_called_with("ABCD", 2)
            print("CASE PASS chart follow-up reuses the explicit symbol from this conversation")
        page.set_viewport_size({"width": 1280, "height": 720})

        page.get_by_role("button", name="Sign out").click()
        expect(page).to_have_url(re.compile(r"/login(?:\?|$)"))
        response = page.context.request.get(base + "/api/conversations")
        assert response.status == 401, response.status
        print("CASE PASS sign-out revokes browser access to conversations")


if __name__ == "__main__":
    main()
