"""Isolated browser checks for assignment, questions, permissions and recovery."""
import contextlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import uuid
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from agent_tasks import Manager, MARKER
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

with contextlib.ExitStack() as stack:
 directory=stack.enter_context(tempfile.TemporaryDirectory(prefix='hearth-agent-browser-'))
 fake=Mock();model={'providerID':'fastflowlm','id':'flm/qwen3-it:4b'}
 fake.models.return_value=[{**model,'name':'Fixture Qwen'}];fake.create.return_value='ses_browser'
 snap={'active':True,'forms':[],'permissions':[],'inbox':[],'messages':[]}
 fake.snapshot.side_effect=lambda _:json.loads(json.dumps(snap))
 manager=Manager(Path(directory)/'tasks',fake)
 manager.thread=Mock();manager.thread.is_alive.return_value=True
 stack.enter_context(patch.object(app,'_agent_tasks',manager))
 stack.enter_context(patch.object(app._model_test_registry,'list',return_value=[]))
 stack.enter_context(patch.object(app.api_list_models,'__wrapped__',return_value=Mock(get_json=lambda:{'models':[{'id':model['id'],'backend':model['providerID'],'state':'loaded'}]})))
 owner=app.users_store.create_user('browser-agent-'+uuid.uuid4().hex,'agent-password-1','admin')['id']
 stack.callback(app.users_store.delete_user,owner)
 stack.enter_context(patch.dict(app.app.config,{'SESSION_COOKIE_SECURE':False}))
 server=make_server('127.0.0.1',0,app.app,threaded=True)
 threading.Thread(target=server.serve_forever,daemon=True).start();stack.callback(server.shutdown)
 client=app.app.test_client()
 with client.session_transaction() as session:
  session.update(logged_in=True,user_id=owner,username='browser-agent',role='admin',last_activity=time.time(),login_csrf_token='csrf-browser')
 with sync_playwright() as p:
  browser=p.chromium.launch(headless=True,args=['--no-sandbox']);context=browser.new_context(viewport={'width':1280,'height':1000})
  context.add_cookies([{'name':app.app.config['SESSION_COOKIE_NAME'],'value':client.get_cookie(app.app.config['SESSION_COOKIE_NAME']).value,'domain':'127.0.0.1','path':'/'}])
  page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  page.on('dialog',lambda dialog:dialog.accept())
  # The model picker lives on the New agent page, not the assignment list.
  page.goto(f'http://127.0.0.1:{server.server_port}/agents/new')
  expect(page.locator('#agent-model option')).to_have_count(1)
  page.locator('[name=title]').fill('Build <img src=x onerror=alert(1)> project')
  page.locator('[name=goal]').fill('Build the feature and verify the result.')
  page.locator('details.agent-more-options summary').click()
  page.locator('[name=verification_command]').fill('pytest -q')
  page.locator('#agent-assign').click()
  expect(page.locator('#agent-status')).to_contain_text('queued',timeout=7000)
  job=manager.list()[0];key=job['id'];manager.tick()
  # Snapshot active is initially true, so no prompt has been sent yet. Turn the
  # empty new session idle to dispatch its first goal.
  snap['active']=False;manager.tick();snap['active']=True
  form={'id':'form_browser','title':'Implementation choices','fields':[
   {'key':'database','type':'string','title':'Database','required':True,'default':'sqlite','options':[{'label':'SQLite','value':'sqlite'},{'label':'PostgreSQL','value':'postgres'}]},
   {'key':'features','type':'multiselect','title':'Features','options':[{'label':'API','value':'api'},{'label':'CLI','value':'cli'}],'custom':True,'default':['api']},
   {'key':'timeout','type':'integer','title':'Optional timeout'},
   {'key':'keep_tests','type':'boolean','title':'Keep tests','default':True}]}
  snap['forms']=[form];manager.tick()
  expect(page.get_by_text('Implementation choices',exact=True)).to_be_visible(timeout=7000)
  expect(page.get_by_label('Database',exact=True)).to_have_value('sqlite')
  expect(page.get_by_label('Keep tests',exact=True)).to_be_checked()
  page.get_by_label('Database',exact=True).select_option('postgres')
  page.get_by_label('API',exact=True).check();page.get_by_label('CLI',exact=True).check()
  page.get_by_role('button',name='Answer and continue').click()
  expect(page.locator('#agent-status')).to_contain_text('running')
  fake.reply.assert_called_with('ses_browser','form','form_browser',{'database':'postgres','features':['api','cli'],'keep_tests':True})
  snap['forms']=[];snap['permissions']=[{'id':'perm_browser','action':'bash','resources':['pytest -q']}];manager.tick()
  expect(page.get_by_role('button',name='Allow once')).to_be_visible(timeout=7000)
  page.get_by_role('button',name='Allow once').click();expect(page.locator('#agent-status')).to_contain_text('running');fake.reply.assert_called_with('ses_browser','permission','perm_browser','once')
  snap['permissions']=[]
  page.locator('#agent-steering').fill('Also add focused tests.')
  page.locator('#agent-send-instructions').click()
  expect(page.locator('#agent-steering')).to_have_value('')
  manager.tick();assert 'Also add focused tests.' in fake.prompt.call_args.args[1]
  expect(page.locator('#agent-reset-cycles')).to_be_visible()
  page.locator('#agent-pause').click();expect(page.locator('#agent-status')).to_contain_text('paused')
  expect(page.locator('#agent-reset-cycles')).to_be_visible()
  page.locator('#agent-reset-cycles').click()
  expect(page.locator('#agent-status')).to_contain_text('0/50 cycles')
  page.close();page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda dialog:dialog.accept())
  page.goto(f'http://127.0.0.1:{server.server_port}/agents/{key}',wait_until='domcontentloaded')
  expect(page.locator('#agent-status')).to_contain_text('paused')
  expect(page.locator('#agent-status')).to_contain_text('0/50 cycles')
  page.locator('#agent-resume').click();expect(page.locator('#agent-status')).to_contain_text('queued')
  snap['active']=True;manager.tick()
  fake.message.return_value={'status':'exited','exit':0,'output':{'output':'tests passed'}}
  snap['active']=False;snap['messages']=[{'id':'a_browser','type':'assistant','finish':'stop','content':[{'type':'text','text':'Verified result\n'+MARKER}]}]
  manager.tick();manager.tick()
  expect(page.locator('#agent-status')).to_contain_text('completed',timeout=7000)
  expect(page.locator('#agent-result-heading')).to_have_text('Completed result')
  expect(page.locator('#agent-result')).to_contain_text('Verified result')
  expect(page.locator('#agent-result')).to_contain_text('Verified by the assigned command')
  expect(page.get_by_role('heading',name='Conversation and tool logs')).to_be_visible()
  current=manager.jobs[key]
  current['activity']=[{'time':time.time()-60,'text':'Older activity event'},
                       {'time':time.time(),'text':'Newest activity event'}]
  manager.save(current)
  page.reload(wait_until='domcontentloaded')
  expect(page.locator('#agent-activity-feed .agent-activity-item span').first).to_have_text('Newest activity event')
  full_activity=page.locator('#agent-activity').text_content()
  assert full_activity.index('Newest activity event') < full_activity.index('Older activity event')
  current['activity'].append({'time':time.time(),'text':'Activity added after page load'})
  current['messages'].append({'id':'a_late_browser','type':'assistant','time':time.time(),
                              'content':[{'type':'text','text':'Transcript added after page load'}]})
  manager.save(current)
  expect(page.locator('#agent-activity-feed')).to_contain_text('Activity added after page load',timeout=7000)
  expect(page.locator('#agent-messages')).to_contain_text('Transcript added after page load',timeout=7000)
  expect(page.get_by_role('button',name='Run again')).to_be_visible()
  page.get_by_role('button',name='Run again').click()
  expect(page.locator('#agent-status')).to_contain_text('queued')
  manager.tick()
  rerun_job=manager.get(key)
  assert rerun_job['status']=='running',rerun_job
  assert fake.create.call_count==2
  page.reload(wait_until='domcontentloaded')
  expect(page.locator('#agent-status')).to_contain_text('running')
  expect(page.locator('#agent-activity')).to_contain_text('Rerun queued')
  assert page.locator('#agent-title img').count()==0
  assert not errors,errors
  page.screenshot(path='/tmp/hearth-agents-ui.png',full_page=True)
  browser.close()
  print('PASS browser assignment, questions, multiple choices, permission reply, pause, reload, resume, verification and escaping')
