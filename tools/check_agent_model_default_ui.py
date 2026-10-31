"""Browser regression: the resume model picker defaults to the loaded coding model."""
import contextlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import agent_tasks
from agent_tasks import Manager
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

CURRENT = {'providerID': 'lmstudio', 'id': 'gemma4-coding-agent'}
LOADED = {'providerID': 'lmstudio', 'id': 'qwen-coding-agent'}

with contextlib.ExitStack() as stack:
    directory = stack.enter_context(tempfile.TemporaryDirectory(prefix='hearth-agent-model-ui-'))
    fake = Mock()
    fake.models.return_value = [
        {**CURRENT, 'name': 'Gemma current assignment'},
        {**LOADED, 'name': 'Loaded Qwen'},
    ]
    stack.enter_context(patch.object(agent_tasks.agent_store, 'list_tasks', return_value=[]))
    stack.enter_context(patch.object(agent_tasks.agent_store, 'save_task'))
    stack.enter_context(patch.object(agent_tasks.agent_store, 'user_exists', return_value=True))
    manager = Manager(Path(directory) / 'tasks', fake)
    task = manager.create({'title': 'Model picker check', 'goal': 'Check the selected model', 'model': CURRENT}, 'browser')
    manager.jobs[task['id']].update(status='blocked', error='UI test fixture')
    manager.save(manager.jobs[task['id']])
    stack.enter_context(patch.object(app, '_agent_tasks', manager))
    stack.enter_context(patch.object(app, '_model_test_registry', Mock(list=Mock(return_value=[]))))
    stack.enter_context(patch.object(app.users_store, 'get_user_by_id', return_value={'id': 'browser', 'username': 'browser', 'role': 'admin'}))
    stack.enter_context(patch.object(app.api_list_models, '__wrapped__', return_value=Mock(get_json=lambda: {
        'models': [{'id': LOADED['id'], 'backend': LOADED['providerID'], 'state': 'loaded', 'type': 'llm'}]
    })))
    stack.enter_context(patch.dict(app.app.config, {'SESSION_COOKIE_SECURE': False}))

    server = make_server('127.0.0.1', 0, app.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stack.callback(server.shutdown)
    client = app.app.test_client()
    with client.session_transaction() as session:
        session.update(logged_in=True, user_id='browser', username='browser', role='admin',
                       last_activity=time.time(), login_csrf_token='csrf-browser')
    cookie = client.get_cookie(app.app.config['SESSION_COOKIE_NAME']).value

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': 1280, 'height': 900})
        context.add_cookies([{'name': app.app.config['SESSION_COOKIE_NAME'], 'value': cookie, 'domain': '127.0.0.1', 'path': '/'}])
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(f'http://127.0.0.1:{server.server_port}/agents/{task["id"]}')
        picker = page.locator('#agent-resume-model')
        expect(picker.locator('option')).to_have_count(2)
        expect(picker).to_have_value(json.dumps(LOADED, separators=(',', ':')))
        expect(picker.locator('option:checked')).to_have_text('Loaded Qwen')
        assert not errors, errors
        print('PASS resume model picker selects Loaded Qwen while assignment model is Gemma')
        browser.close()
