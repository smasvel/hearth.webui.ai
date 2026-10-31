"""Browser regression for model-test controls using isolated simulated models. Requires Playwright Chromium."""
import contextlib
import sys
import threading
import time
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright, expect
import app
import model_tests
import model_test_profiles as profiles

TARGET = {'backend':'fastflowlm','id':'flm/test-fixture','state':'not-loaded','type':'llm'}
with contextlib.ExitStack() as stack:
 stack.enter_context(patch.dict(app.app.config, {'SESSION_COOKIE_SECURE':False}))
 registry=model_tests.Registry()
 stack.callback(lambda: [registry.delete(job_id) for job_id in list(registry.jobs)
                          if registry.jobs[job_id].get('status') not in model_tests.ACTIVE])
 values={}; mode={'wait':False}; passes={'n':0}
 stack.enter_context(patch.object(app,'_model_test_registry',registry))
 stack.enter_context(patch.object(app,'_test_model_inventory',return_value=[TARGET]))
 stack.enter_context(patch.object(app,'_test_load_model'))
 stack.enter_context(patch.object(app,'_test_connection',return_value=('http://fixture','test-fixture',{})))
 stack.enter_context(patch.object(app,'_test_context',return_value=8192))
 stack.enter_context(patch.object(app.users_store,'get_user_by_id',return_value={'id':'ui-test','username':'ui-test','role':'admin'}))
 for client in (app.lms,app.lemonade,app.vllm,app.sglang,app.kobold):
  stack.enter_context(patch.object(client,'list_models',side_effect=lambda:[]))
 stack.enter_context(patch.object(app.fastflowlm,'list_models',side_effect=lambda:[TARGET.copy()]))
 stack.enter_context(patch.object(app.comfyui,'video_model_info',return_value={'id':'media','backend':'comfyui','type':'video'}))
 stack.enter_context(patch.object(profiles,'get',side_effect=lambda b,m:values.get((b,m.removeprefix('flm/')))))
 stack.enter_context(patch.object(profiles,'set_profile',side_effect=lambda b,m,v:values.__setitem__((b,m.removeprefix('flm/')),v)))
 def suite(model,url,**kw):
  if mode['wait']:
   kw.get('on_case',lambda c:None)({'name':'waiting-check','stream':False,'started_at':time.time()})
   kw['on_result']({'name':'plain-chat','passed':True,'stream':False,'seconds':.01})
   kw['cancel'].wait(8)
   raise model_tests.Cancelled()
  passes['n']+=1
  rows=[{'name':'chat','passed':True,'stream':False,'seconds':.1},
        {'name':'tools','passed':passes['n']%2==0,'stream':True,'seconds':.1,
         'error':'<img src=x onerror=window.badInjection=true>' if passes['n']%2 else ''},
        {'name':'opencode-choice-resume','passed':passes['n']%2==0,'stream':True,'seconds':0,
         'skipped':passes['n']%2!=0,'blocked_by':'tools' if passes['n']%2 else None,
         'error':'Dependent step skipped after tools' if passes['n']%2 else ''}]
  for row in rows:
   kw['on_result'](row);time.sleep(.15)
  return rows
 stack.enter_context(patch.object(model_tests,'run_suite',side_effect=suite))
 server=make_server('127.0.0.1',0,app.app,threaded=True)
 thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
 stack.callback(server.shutdown)
 client=app.app.test_client()
 with client.session_transaction() as s:
  s.update(logged_in=True,user_id='ui-test',username='ui-test',role='admin',last_activity=time.time(),login_csrf_token='browser-csrf')
 cookie=client.get_cookie(app.app.config['SESSION_COOKIE_NAME']).value
 with sync_playwright() as p:
  browser=p.chromium.launch(headless=True,args=['--no-sandbox'])
  context=browser.new_context(viewport={'width':1280,'height':1000})
  context.add_cookies([{'name':app.app.config['SESSION_COOKIE_NAME'],'value':cookie,'domain':'127.0.0.1','path':'/'}])
  page=context.new_page();errors=[]
  page.on('pageerror',lambda error:errors.append(str(error)))
  page.on('dialog',lambda dialog:dialog.accept())
  page.goto(f'http://127.0.0.1:{server.server_port}/models')
  expect(page.locator('#model-tests')).to_have_count(0)
  expect(page.locator('#model-groups')).to_be_visible()
  page.goto(f'http://127.0.0.1:{server.server_port}/model-tests')
  expect(page.locator('#tests-auto-repair')).not_to_be_checked()
  page.locator('#tests-auto-repair').check()
  page.get_by_label('Model to test').select_option(label='fastflowlm · flm/test-fixture')
  page.get_by_role('button',name='Run tests for selected model').click()
  expect(page.locator('#tests-status')).to_contain_text('completed',timeout=12000)
  expect(page.locator('#tests-status')).to_contain_text('3 passed · 0 failed')
  expect(page.locator('#tests-repairs')).to_contain_text('Applied and verified')
  assert page.locator('#tests-results img').count()==0
  assert page.evaluate('window.badInjection') is None
  href=page.locator('#tests-download').get_attribute('href')
  downloaded=context.request.get(f'http://127.0.0.1:{server.server_port}'+href)
  assert downloaded.status==200 and len(downloaded.json()['results'])==6
  assert page.locator('#tests-results').get_by_text('Blocked',exact=True).count()==1
  page.reload();expect(page.locator('#tests-status')).to_contain_text('completed')
  page.goto(f'http://127.0.0.1:{server.server_port}/models')
  page.get_by_role('button',name='Reset repair',exact=True).click()
  expect(page).to_have_url(__import__('re').compile(r'.*/model-tests$'))
  expect(page.locator('#tests-status')).to_contain_text('Repair profile reset')
  assert values[('fastflowlm','test-fixture')] is None
  old_report_id = page.locator('#tests-history').input_value()
  mode['wait']=True
  page.get_by_label('Model to test').select_option(index=1)
  page.get_by_role('button',name='Run tests for selected model').click()
  expect(page.locator('#tests-cancel')).to_be_enabled()
  expect(page.locator('#tests-status')).to_contain_text('waiting-check')
  expect(page.locator('#tests-delete-history option')).to_have_count(1)
  page.get_by_role('button',name='Delete selected report').click()
  expect(page.locator('#tests-delete-status')).to_contain_text('Older report deleted. The active model test continues.')
  expect(page.locator('#tests-status')).to_contain_text('waiting-check')
  expect(page.locator('#tests-history option')).to_have_count(1)
  assert context.request.get(f'http://127.0.0.1:{server.server_port}/api/model-tests/{old_report_id}').status == 404
  page.locator('#tests-cancel').click()
  expect(page.locator('#tests-status')).to_contain_text('cancelled',timeout=10000)
  assert not errors,errors
  page.screenshot(path='/tmp/hearth-model-tests-ui.png',full_page=True)
  browser.close()
  print('PASS browser start, live progress, repair, escaping, report download, reload recovery, reset and cancellation')
