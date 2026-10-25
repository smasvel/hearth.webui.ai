"""Isolated resource-warning layout and filtering checks. No models are loaded."""
import contextlib
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import model_resource_warnings
import model_metadata_store
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server


with contextlib.ExitStack() as stack:
    catalog = model_metadata_store.resource_warnings()
    models = [{'backend': backend, 'id': item['model'], 'state': 'not-loaded', 'type': 'llm'}
              for (backend, _), item in catalog.items()]
    models.append({'backend': 'lmstudio', 'id': 'qwen/qwen3.5-4b', 'state': 'not-loaded', 'type': 'llm'})
    for backend, client in {'lmstudio': app.lms, 'lemonade': app.lemonade, 'vllm': app.vllm,
                            'kobold': app.kobold, 'fastflowlm': app.fastflowlm, 'sglang': app.sglang}.items():
        stack.enter_context(patch.object(client, 'list_models', side_effect=lambda key=backend:
                                        [dict(m) for m in models if m['backend'] == key]))
        stack.enter_context(patch.object(client, 'base_url', return_value='http://127.0.0.1:9999'))
    stack.enter_context(patch.object(app.comfyui, 'video_model_info', return_value={'id': 'media', 'backend': 'comfyui', 'type': 'video'}))
    stack.enter_context(patch.object(app._model_test_registry, 'list', return_value=[]))
    stack.enter_context(patch.object(app.users_store, 'get_user_by_id', return_value={'id': 'ui-test', 'username': 'ui-test', 'role': 'admin'}))
    stack.enter_context(patch.dict(app.app.config, {'SESSION_COOKIE_SECURE': False}))
    server = make_server('127.0.0.1', 0, app.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stack.callback(server.shutdown)
    client = app.app.test_client()
    with client.session_transaction() as session:
        session.update(logged_in=True, user_id='ui-test', username='ui-test', role='admin', last_activity=time.time())
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = browser.new_context()
        context.add_cookies([{'name': 'session', 'value': client.get_cookie('session').value, 'domain': '127.0.0.1', 'path': '/'}])
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(f'http://127.0.0.1:{server.server_port}/models')
        expect(page.locator('.model-resource-warning')).to_have_count(10)
        expect(page.locator('.model-card2')).to_have_count(12)
        # The summary text is now always visible in the actions row next to the Load
        # button (.model-load-warning); only the evidence list stays collapsed.
        expect(page.locator('.model-load-warning')).to_have_count(10)
        expect(page.locator('.model-load-warning').filter(has_text='32.8 GiB')).to_be_visible()
        page.get_by_label('Resource warnings only').check()
        expect(page.locator('.model-card2')).to_have_count(10)
        for width in (320, 390, 768, 1280):
            page.set_viewport_size({'width': width, 'height': 900})
            warning = page.locator('.model-load-warning').filter(has_text='32.8 GiB')
            warning.locator('summary').click()
            expect(warning.locator('ul')).to_be_visible()
            warning.scroll_into_view_if_needed()
            assert page.locator('.console-body').evaluate('(e) => e.scrollWidth <= e.clientWidth'), width
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
            page.screenshot(path=f'/tmp/hearth-model-warnings-{width}.png')
            warning.locator('summary').click()
        page.get_by_label('Filter models by name').fill('qwen3.8')
        expect(page.locator('.model-card2')).to_have_count(3)
        page.get_by_label('Filter models by name').fill('qwen3.5')
        expect(page.locator('.model-card2')).to_have_count(0)
        page.get_by_label('Resource warnings only').uncheck()
        expect(page.locator('.model-card2')).to_have_count(1)
        assert page.locator('.model-resource-warning img').count() == 0
        assert not errors, errors
        browser.close()
        print('PASS resource warnings, evidence, backend attribution, filtering, and mobile/desktop layout')
