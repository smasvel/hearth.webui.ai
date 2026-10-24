"""Playwright regression for admin settings, account management, and app navigation."""
import contextlib
import sys
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import database
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

with contextlib.ExitStack() as stack:
    admin = app.users_store.create_user('browser-admin-' + uuid.uuid4().hex, 'admin-password-1', 'admin')
    stack.callback(app.users_store.delete_user, admin['id'])
    stack.enter_context(patch.dict(app.app.config, {'SESSION_COOKIE_SECURE': False}))
    stack.enter_context(patch.object(app, '_test_model_inventory', return_value=[]))
    for backend in (app.lms, app.lemonade, app.vllm, app.sglang, app.kobold, app.fastflowlm):
        stack.enter_context(patch.object(backend, 'list_models', return_value=[]))
    stack.enter_context(patch.object(app.comfyui, 'video_model_info', return_value={
        'id': 'fixture', 'backend': 'comfyui', 'type': 'video', 'size_gb': 0,
        'publisher': 'test', 'quantization': 'test', 'state': 'not-loaded', 'downloaded': False,
    }))
    stack.enter_context(patch.object(app.help_data, 'tailscale_ip', return_value=None))
    server = make_server('127.0.0.1', 0, app.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stack.callback(server.shutdown)
    client = app.app.test_client()
    with client.session_transaction() as session:
        session.update(logged_in=True, user_id=admin['id'], username=admin['username'], role='admin',
                       last_activity=time.time(), login_csrf_token='browser-csrf')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': 1280, 'height': 1000})
        context.add_cookies([{'name': 'session', 'value': client.get_cookie('session').value,
                              'domain': '127.0.0.1', 'path': '/'}])
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('dialog', lambda dialog: dialog.accept('reset-password-2'))

        page.goto(f'http://127.0.0.1:{server.server_port}/settings')
        expect(page.locator('#settings-save-btn')).to_be_visible()
        page.locator('#settings-lmstudio-url').fill('http://127.0.0.1:1234')
        page.locator('#settings-lmstudio-key').fill('browser-secret-token')
        page.locator('#settings-web-search-max-results').fill('5')
        page.locator('#settings-save-btn').click()
        expect(page.locator('.users-toast')).to_contain_text('Settings saved', timeout=7000)
        assert app.settings_store.get('lmstudio_base_url') == 'http://127.0.0.1:1234'
        assert app.settings_store.get('lmstudio_api_key') == 'browser-secret-token'
        with database.connect() as conn:
            stored = conn.execute("SELECT value FROM settings WHERE key='lmstudio_api_key'").fetchone()['value']
        assert stored.startswith('v1:') and 'browser-secret-token' not in stored
        page.reload()
        expect(page.locator('#settings-lmstudio-key')).to_have_value('browser-secret-token')

        page.goto(f'http://127.0.0.1:{server.server_port}/users')
        page.locator('#new-username').fill('browser-created-user')
        page.locator('#new-password').fill('initial-password-1')
        page.locator('#new-agentic-workflow').select_option('true')
        page.locator('#add-user-btn').click()
        card = page.locator('#users-list .model-card').filter(has_text='browser-created-user')
        expect(card).to_be_visible(timeout=7000)
        row = next(user for user in app.users_store.list_users()['users'] if user['username'] == 'browser-created-user')
        created_id = row['id']
        stack.callback(app.users_store.delete_user, created_id)
        with database.connect() as conn:
            password_hash = conn.execute('SELECT password_hash FROM users WHERE id=%s', (created_id,)).fetchone()['password_hash']
        assert password_hash != 'initial-password-1'
        card.get_by_role('button', name='LAN token').click()
        expect(page.locator('#caddy-snippet-card')).to_be_visible()
        old_token = app.users_store.get_caddy_token(created_id)
        card.get_by_role('button', name='Regenerate token').click()
        expect(page.locator('#caddy-snippet-text')).not_to_contain_text(old_token, timeout=5000)
        assert app.users_store.get_caddy_token(created_id) != old_token
        card.get_by_role('button', name='Reset password').click()
        expect(page.locator('.users-toast').filter(has_text='Password reset')).to_be_visible(timeout=5000)
        assert app.users_store.verify_login('browser-created-user', 'reset-password-2')
        card.get_by_role('button', name='Disable agent workflow').click()
        expect(card).to_contain_text('Disabled', timeout=5000)

        for route in ('/', '/models', '/downloads', '/metrics', '/agents', '/agents/new',
                      '/agents/schedules', '/browse-history', '/quizzes', '/dmv', '/flm-converter'):
            response = context.request.get(f'http://127.0.0.1:{server.server_port}{route}')
            assert response.status == 200, f'{route} returned {response.status}'
        assert not errors, errors
        page.screenshot(path='/tmp/hearth-admin-ui.png', full_page=True)
        browser.close()
        print('PASS browser Settings persistence and secret encryption, user create/reset/token/workflow controls, and admin route access')
