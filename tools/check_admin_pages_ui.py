"""Admin browser smoke and interaction coverage across every standalone page."""
import contextlib
import sys
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import browse_history_store
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server


with contextlib.ExitStack() as stack:
    admin = app.users_store.create_user('browser-pages-' + uuid.uuid4().hex, 'admin-password-1', 'admin')
    stack.callback(app.users_store.delete_user, admin['id'])
    stack.enter_context(patch.dict(app.app.config, {'SESSION_COOKIE_SECURE': False}))
    stack.enter_context(patch.object(app, '_test_model_inventory', return_value=[]))
    for backend in (app.lms, app.lemonade, app.vllm, app.sglang, app.kobold, app.fastflowlm):
        stack.enter_context(patch.object(backend, 'list_models', return_value=[]))
    stack.enter_context(patch.object(app.comfyui, 'video_model_info', return_value={
        'id': 'fixture-video', 'backend': 'comfyui', 'type': 'video', 'size_gb': 0,
        'publisher': 'test', 'quantization': 'test', 'state': 'not-loaded', 'downloaded': False,
    }))
    stack.enter_context(patch.object(app.help_data, 'tailscale_ip', return_value=None))
    stack.enter_context(patch.object(app.flm_conversion, 'availability', return_value={
        'available': False, 'missing': ['test fixture'], 'output_root': '/tmp',
        'supported_profile': {}, 'backend_profile': {},
    }))
    stack.enter_context(patch.object(app.flm_conversion, 'discover_models', return_value=[]))
    stack.enter_context(patch.object(app.flm_conversion, 'list_jobs', return_value=[]))
    server = make_server('127.0.0.1', 0, app.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stack.callback(server.shutdown)
    client = app.app.test_client()
    with client.session_transaction() as session:
        session.update(logged_in=True, user_id=admin['id'], username=admin['username'], role='admin',
                       last_activity=time.time(), login_csrf_token='browser-pages-csrf')

    # Seed visits so history covers search and both deletion controls.
    browse_history_store.add_visit('https://example.test/admin-coverage', 'Admin coverage fixture',
                                   'page smoke coverage', 'ui-test', True)
    browse_history_store.add_visit('https://example.test/delete-row', 'Delete row fixture',
                                   'row delete coverage', 'ui-test', True)
    browse_history_store.add_visit('https://example.test/delete-all', 'Delete all fixture',
                                   'clear history coverage', 'ui-test', True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        context.add_cookies([{'name': 'session', 'value': client.get_cookie('session').value,
                              'domain': '127.0.0.1', 'path': '/'}])
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: errors.append('console: ' + message.text) if message.type == 'error' else None)
        base = f'http://127.0.0.1:{server.server_port}'

        # Exercise browser rendering and each page's expected primary controls.
        checks = {
            '/': ('Chats', '#model-select'),
            '/models': ('Models', '#model-groups'),
            '/downloads': ('Download Models', '#all-search-form'),
            '/model-tests': ('Model tests & repairs', '#tests-run'),
            '/flm-converter': ('FLM NPU Converter', '#converter-form'),
            '/browse-history': ('AI Browser History', '#history-search'),
            '/quizzes': ('Build your own practice tests', '#save-deck'),
            '/metrics': ('Performance metrics', '#kpis'),
            '/live-logs': ('Live developer logs', '#live-feed'),
            '/users': ('Users', '#add-user-form'),
            '/settings': ('Settings', '#settings-form'),
            '/agents': ('What are you working on?', '.agent-nav'),
            '/agents/new': ('What should your agent do?', '[name="title"]'),
            '/agents/schedules': ('Schedules', '.agent-nav'),
            '/dmv': ('DMV Practice Test', '#dmv-start-btn'),
        }
        for route, (heading, selector) in checks.items():
            response = page.goto(base + route)
            assert response and response.status == 200, f'{route} returned {response.status if response else None}'
            expect(page.get_by_role('heading', name=heading).first).to_be_visible()
            expect(page.locator(selector).first).to_be_visible()

        # Local GGUF upload belongs to Models, reports progress, refreshes the model
        # inventory after registration, and is absent from Downloads.
        page.goto(base + '/models')
        expect(page.get_by_role('heading', name='Upload a local GGUF model')).to_be_visible()
        expect(page.locator('#local-model-file')).to_have_attribute('accept', '.gguf,application/octet-stream')
        expect(page.locator('#local-model-upload-progress')).to_have_class(__import__('re').compile(r'progress-bar-track hidden'))
        upload_requests = []
        def mock_upload(route):
            upload_requests.append(route.request.headers.get('x-csrf-token'))
            route.fulfill(status=200, content_type='application/json', body='{"ok":true,"model_id":"user.fixture-model","size_bytes":2097152}')
        page.route('**/api/models/upload-local', mock_upload)
        page.set_input_files('#local-model-file', {'name': 'fixture-model.gguf', 'mimeType': 'application/octet-stream', 'buffer': b'GGUFfixture'})
        page.get_by_role('button', name='Upload model').click()
        expect(page.locator('#local-model-upload-status')).to_contain_text('user.fixture-model uploaded and registered (2 MB).')
        expect(page.locator('#local-model-upload-progress')).not_to_have_class(__import__('re').compile(r'\bhidden\b'))
        expect(page.locator('#local-model-file')).to_have_value('')
        expect(page.locator('#model-summary')).to_be_visible()
        assert upload_requests == ['browser-pages-csrf'], upload_requests
        page.unroute('**/api/models/upload-local')
        page.goto(base + '/downloads')
        expect(page.get_by_role('heading', name='Upload a local GGUF model')).to_have_count(0)
        expect(page.locator('#local-model-upload-form')).to_have_count(0)

        # Use a fresh browser context so a prior Metrics visit cannot hide a page-specific
        # script initialization problem behind the browser cache.
        live_context = browser.new_context()
        live_context.add_cookies([{'name': 'session', 'value': client.get_cookie('session').value,
                                   'domain': '127.0.0.1', 'path': '/'}])
        live_page = live_context.new_page()
        live_page.on('pageerror', lambda error: errors.append(str(error)))
        live_rows = [{
            'id': 987654, 'source': 'ui-test', 'backend': 'fastflowlm', 'model': 'fixture-model',
            'is_npu': True, 'tokens': 12, 'tokens_per_sec': 4.0, 'elapsed_s': 3.0,
            'status': 'done', 'started_at': time.time() - 3, 'finished_at': time.time(),
        }]
        stack.enter_context(patch.object(app.live_requests, 'snapshot', return_value=live_rows))
        stack.enter_context(patch.object(app.live_requests, 'trace', return_value={
            'request_content': 'fixture request text', 'response_content': 'fixture response text',
            'response_truncated': False,
        }))
        live_page.goto(base + '/live-logs')
        expect(live_page.locator('#live-feed')).to_contain_text('fixture-model', timeout=5000)
        expect(live_page.locator('#npu-status')).to_contain_text('NPU idle', timeout=5000)
        live_page.get_by_text('View request and response').click()
        expect(live_page.locator('#live-feed')).to_contain_text('fixture request text', timeout=5000)
        expect(live_page.locator('#live-feed')).to_contain_text('fixture response text')
        live_context.close()

        # Metrics: backend-free refresh and range change must populate all major regions.
        page.goto(base + '/metrics')
        expect(page.locator('#recent-count')).to_contain_text('No matching requests', timeout=5000)
        expect(page.locator('#model-report')).to_contain_text('No generation requests')
        page.locator('#range').select_option('1')
        page.get_by_role('button', name='Refresh').click()
        expect(page.locator('#kpis')).to_contain_text('Requests')

        # History search hits the seeded row and filters it out for a missing term.
        page.goto(base + '/browse-history')
        expect(page.locator('#history-rows')).to_contain_text('Admin coverage fixture', timeout=5000)
        page.locator('#history-search').fill('fixture-no-match')
        expect(page.locator('#history-empty')).to_be_visible(timeout=5000)
        expect(page.locator('#history-count')).to_contain_text('0 matches')
        page.locator('#history-search').fill('Delete row fixture')
        row = page.locator('#history-rows tr').filter(has_text='Delete row fixture')
        expect(row).to_be_visible(timeout=5000)
        page.once('dialog', lambda dialog: dialog.accept())
        row.get_by_role('button', name='Delete history entry for Delete row fixture').click()
        expect(page.locator('#history-action-status')).to_contain_text('History entry deleted.', timeout=5000)
        page.locator('#history-search').fill('')
        expect(page.locator('#history-rows')).to_contain_text('Delete all fixture', timeout=5000)
        page.once('dialog', lambda dialog: dialog.accept())
        page.locator('#history-clear-all').click()
        expect(page.locator('#history-action-status')).to_contain_text('Deleted 2 history entries.', timeout=5000)
        expect(page.locator('#history-empty')).to_be_visible(timeout=5000)

        # Quiz builder adds and removes a question without network or model dependencies.
        page.goto(base + '/quizzes')
        page.get_by_role('button', name='Add question').click()
        expect(page.locator('.quizb-q')).to_have_count(2)
        page.locator('.quizb-q').last.get_by_role('button', name='Remove').click()
        expect(page.locator('.quizb-q')).to_have_count(1)

        # DMV page loads the question catalog and begins a practice test.
        page.goto(base + '/dmv')
        expect(page.locator('#dmv-state option')).not_to_have_count(0, timeout=5000)
        page.locator('#dmv-start-btn').click()
        expect(page.locator('#dmv-quiz')).to_be_visible(timeout=5000)
        expect(page.locator('#dmv-options button')).not_to_have_count(0)

        # Admin screens and admin-only APIs must redirect/reject a normal user.
        normal = app.users_store.create_user('browser-regular-' + uuid.uuid4().hex, 'user-password-1', 'user')
        stack.callback(app.users_store.delete_user, normal['id'])
        with client.session_transaction() as session:
            session.update(user_id=normal['id'], username=normal['username'], role='user', last_activity=time.time())
        context.clear_cookies()
        context.add_cookies([{'name': 'session', 'value': client.get_cookie('session').value,
                              'domain': '127.0.0.1', 'path': '/'}])
        for route in ('/users', '/settings', '/downloads', '/models', '/model-tests', '/flm-converter',
                      '/browse-history', '/quizzes', '/metrics', '/live-logs'):
            response = context.request.get(base + route, max_redirects=0)
            assert response.status in (302, 303, 403), f'{route} was not admin protected: {response.status}'
        assert context.request.get(base + '/api/metrics/live').status in (302, 403)

        assert not errors, errors
        browser.close()
        print('PASS admin browser coverage for all standalone pages, key interactions, and admin route guards')
