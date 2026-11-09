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
    for backend in (app.lms, app.lemonade, app.vllm, app.kobold, app.fastflowlm):
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
    # Clear first so repeated runs don't accumulate duplicate rows (the store is shared
    # with the dev/production DB, so leftover fixture rows would break strict-mode lookups).
    browse_history_store.clear_history()
    browse_history_store.add_visit('https://example.test/admin-coverage', 'Admin coverage fixture',
                                   'page smoke coverage', 'ui-test', True)
    browse_history_store.add_visit('https://example.test/delete-row', 'Delete row fixture',
                                   'row delete coverage', 'ui-test', True)
    browse_history_store.add_visit('https://example.test/delete-all', 'Delete all fixture',
                                   'clear history coverage', 'ui-test', True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        context.add_cookies([{'name': app.app.config['SESSION_COOKIE_NAME'], 'value': client.get_cookie(app.app.config['SESSION_COOKIE_NAME']).value,
                              'domain': '127.0.0.1', 'path': '/'}])
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: errors.append('console: ' + message.text) if message.type == 'error' else None)
        base = f'http://127.0.0.1:{server.server_port}'
        page.route('**/api/metrics/model-test-report', lambda route: route.fulfill(
            status=200, content_type='application/json', body=(
                '{"rows":[{"model":"fixture-model","backend":"fixture-backend",'
                '"total":12,"passed":10,"failed":1,"skipped":1,"pass_rate":83.3,'
                '"reasoning_score":75,"tok_s":22.5,"avg_completion_tokens":120,'
                '"avg_seconds":4.2,"score":72.1}],"recommended":null}')))

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

        def check_table_dom(route, viewport_label):
            issues = page.evaluate("""() => {
              const issues = [];
              for (const [tableIndex, table] of [...document.querySelectorAll('table')].entries()) {
                if (table.getClientRects().length === 0) continue;
                const label = table.getAttribute('aria-label') || table.id || `table ${tableIndex + 1} (${table.className})`;
                const headers = [...table.querySelectorAll('thead tr:last-child th')];
                const layoutInfo = `table=${getComputedStyle(table).display},thead=${getComputedStyle(table.tHead).display},headrow=${getComputedStyle(table.tHead.rows[0]).display},tbody=${getComputedStyle(table.tBodies[0]).display},bodyrow=${getComputedStyle(table.tBodies[0].rows[0] || table).display}`;
                for (const [rowIndex, row] of [...table.querySelectorAll('tbody tr')].entries()) {
                  if (row.getClientRects().length === 0) continue;
                  const cells = [...row.children].filter(cell => /^(TD|TH)$/.test(cell.tagName));
                  const expected = headers.reduce((sum, cell) => sum + cell.colSpan, 0);
                  const actual = cells.reduce((sum, cell) => sum + cell.colSpan, 0);
                  if (headers.length && actual !== expected)
                    issues.push(`${label} row ${rowIndex + 1}: ${actual} columns, expected ${expected}`);
                  // Native tables should keep each rendered data column under its header.
                  // Card-style mobile tables intentionally use block layout and are skipped.
                  if (headers.length && cells.length === headers.length &&
                      getComputedStyle(cells[0]).display === 'table-cell') {
                    for (let i = 0; i < cells.length; i++) {
                      const h = headers[i].getBoundingClientRect();
                      const c = cells[i].getBoundingClientRect();
                      if (Math.abs(h.left - c.left) > 2 || Math.abs(h.width - c.width) > 2)
                        issues.push(`${label} ${layoutInfo} row ${rowIndex + 1}, column ${i + 1}: layout=${getComputedStyle(table).tableLayout} width=${table.getBoundingClientRect().width.toFixed(1)} header x=${h.left.toFixed(1)} w=${h.width.toFixed(1)}, cell x=${c.left.toFixed(1)} w=${c.width.toFixed(1)}`);
                    }
                  }
                }
              }
              return issues;
            }""")
            assert not issues, f'{route} ({viewport_label}) table formatting: {issues}'

        # Check all rendered tables at desktop and phone widths, including the blank
        # state rows whose colspan must still match the table's declared columns.
        for route in checks:
            page.set_viewport_size({'width': 1440, 'height': 1000})
            page.goto(base + route)
            page.wait_for_timeout(150)
            check_table_dom(route, 'desktop')
            page.set_viewport_size({'width': 390, 'height': 844})
            page.reload()
            page.wait_for_timeout(150)
            check_table_dom(route, 'mobile')
        page.goto(base + '/metrics')
        expect(page.locator('#model-test-report tr')).to_have_count(1)
        expect(page.locator('.metric-test-report thead th')).to_have_count(12)
        expect(page.locator('.metric-test-report thead')).to_contain_text('Generated tok/s')
        expect(page.locator('.metric-test-report tbody tr').last).to_contain_text('72.1')
        check_table_dom('/metrics', 'populated report')
        page.set_viewport_size({'width': 1440, 'height': 1000})

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
        live_context.add_cookies([{'name': app.app.config['SESSION_COOKIE_NAME'], 'value': client.get_cookie(app.app.config['SESSION_COOKIE_NAME']).value,
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
        # The table window is the shared dev/production store, so seed a clearly-identifiable
        # row (and remove it after) to make the empty/refresh assertions deterministic.
        metrics_rows_before = app.metrics_store.summary(hours=24)['totals']['requests']
        app.metrics_store.record(
            ts=time.time(), source='ui-test', backend='fastflowlm', model='admin-coverage-fixture-model',
            request_type='chat_completion', ask_category='chat', status=200, duration_ms=1500,
            prompt_tokens=7, completion_tokens=5, total_tokens=12, tokens_per_sec=4.0, tops_used=None,
            npu_percent=None, is_npu=True)
        # The model report has no per-model client filter (unlike the recent table), so mock
        # it to show only the seeded fixture row — keeps the empty-state assertion deterministic.
        stack.enter_context(patch.object(app.metrics_store, 'model_report',
                                         return_value=[{'model': 'admin-coverage-fixture-model',
                                                        'backend': 'fastflowlm', 'requests': 1,
                                                        'input_tokens': 7, 'output_tokens': 5,
                                                        'token_samples': 1, 'avg_tps': 4.0,
                                                        'speed_samples': 1, 'avg_ms': 1500.0,
                                                        'succeeded': 1, 'client_errors': 0,
                                                        'server_errors': 0, 'unknown_status': 0,
                                                        'error_rate': None, 'coding_requests': 0,
                                                        'coding_errors': 0, 'reviewed': 0,
                                                        'coding_accuracy': None}]))
        try:
            page.goto(base + '/metrics')
            expect(page.locator('#recent-count')).to_contain_text('of', timeout=5000)
            page.locator('#f-model').fill('no-such-model-xyz')
            expect(page.locator('#recent-count')).to_contain_text('No matching requests', timeout=5000)
            expect(page.locator('#model-report')).to_contain_text('admin-coverage-fixture-model', timeout=5000)
            page.locator('#f-model').fill('admin-coverage-fixture-model')
            expect(page.locator('#recent')).to_contain_text('admin-coverage-fixture-model', timeout=5000)
            page.locator('#f-model').fill('')
            page.locator('#range').select_option('1')
            page.get_by_role('button', name='Refresh').click()
            expect(page.locator('#kpis')).to_contain_text('Requests')
        finally:
            if app.metrics_store.summary(hours=24)['totals']['requests'] > metrics_rows_before:
                app.metrics_store.clear_all()

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
        context.add_cookies([{'name': app.app.config['SESSION_COOKIE_NAME'], 'value': client.get_cookie(app.app.config['SESSION_COOKIE_NAME']).value,
                              'domain': '127.0.0.1', 'path': '/'}])
        for route in ('/users', '/settings', '/downloads', '/models', '/model-tests', '/flm-converter',
                      '/browse-history', '/quizzes', '/metrics', '/live-logs'):
            response = context.request.get(base + route, max_redirects=0)
            assert response.status in (302, 303, 403), f'{route} was not admin protected: {response.status}'
        assert context.request.get(base + '/api/metrics/live').status in (302, 403)

        assert not errors, errors
        browser.close()
        print('PASS admin browser coverage for all standalone pages, key interactions, and admin route guards')
