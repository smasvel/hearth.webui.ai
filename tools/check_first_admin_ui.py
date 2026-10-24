"""Playwright first-visit setup check in a fresh isolated PostgreSQL schema."""
import contextlib
import sys
import threading
import uuid
from pathlib import Path

class _Help(dict):
    def __missing__(self, key):
        value = _Help()
        self[key] = value
        return value

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import database

with database.temporary_schema():
    import app
    from playwright.sync_api import expect, sync_playwright
    from werkzeug.serving import make_server

    app.users_store.init_db()
    with contextlib.ExitStack() as stack:
        stack.enter_context(__import__('unittest.mock', fromlist=['patch']).patch.dict(app.app.config, {'SESSION_COOKIE_SECURE': False}))
        stack.enter_context(__import__('unittest.mock', fromlist=['patch']).patch.object(app.help_data, 'build', return_value=_Help()))
        server = make_server('127.0.0.1', 0, app.app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        stack.callback(server.shutdown)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
            page = browser.new_page()
            page.goto(f'http://127.0.0.1:{server.server_port}/login')
            expect(page.get_by_role('heading', name='Set up Hearth')).to_be_visible()
            username = 'first-admin-' + uuid.uuid4().hex
            page.locator('[name=username]').fill(username)
            page.locator('[name=password]').fill('first-admin-password')
            page.locator('[name=password_confirm]').fill('first-admin-password')
            page.get_by_role('button', name='Create administrator').click()
            expect(page).to_have_url(f'http://127.0.0.1:{server.server_port}/')
            user = app.users_store.verify_login(username, 'first-admin-password')
            assert user and user['role'] == 'admin'
            with database.connect() as conn:
                row = conn.execute('SELECT password_hash FROM users WHERE id=%s', (user['id'],)).fetchone()
            assert row['password_hash'] != 'first-admin-password'
            page.goto(f'http://127.0.0.1:{server.server_port}/login')
            expect(page.get_by_role('heading', name='Hearth')).to_be_visible()
            expect(page.get_by_role('button', name='Sign in')).to_be_visible()
            browser.close()
            print('PASS browser first-visit admin creation, hashed password storage, and one-time setup transition')
