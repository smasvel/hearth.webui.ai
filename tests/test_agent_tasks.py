import copy
import io
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_tasks import Manager, MARKER
import app

MODEL = {'providerID': 'fastflowlm', 'id': 'flm/qwen3-it:4b'}


def assistant(key, text, **extra):
    return {'id': key, 'type': 'assistant', 'finish': 'stop', 'content': [{'type': 'text', 'text': text}], **extra}


class AgentTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.client = Mock()
        self.client.models.return_value = [{**MODEL, 'name': 'Qwen'}]
        self.client.create.return_value = 'ses_fixture'
        self.snap = {'active':False,'forms':[],'permissions':[],'inbox':[],'messages':[]}
        self.client.snapshot.side_effect = lambda _: copy.deepcopy(self.snap)
        self.manager = Manager(Path(self.tmp.name)/'tasks',self.client)
        self.stack=__import__('contextlib').ExitStack()
        self.stack.enter_context(patch('agent_tasks.Path.home', return_value=Path(self.tmp.name)))
        self.addCleanup(self.stack.close)
        self.values = {'title':'Build project','goal':'Build and verify the feature','model':MODEL}

    def create(self, **extra):
        job = self.manager.create({**self.values, **extra}, 'admin')
        self.manager.tick()
        return job['id']

    def finish_turn(self, message):
        self.snap['messages'] = [message]
        self.manager.tick();self.manager.tick()

    def test_actual_assignment_dispatches_and_continues_without_browser(self):
        key = self.create()
        self.client.prompt.assert_called_once()
        self.assertIn(self.values['goal'], self.client.prompt.call_args.args[1])
        self.finish_turn(assistant('a1','First part done; more remains.'))
        self.assertEqual(self.client.prompt.call_count,2)
        self.assertIn(self.values['goal'],self.client.prompt.call_args.args[1])
        self.assertEqual(self.manager.get(key)['cycles'],2)
        self.assertEqual(self.manager.get(key)['status'],'running')

    def test_restart_reconnects_active_session_without_duplicate_prompt(self):
        key = self.create();self.snap['active']=True
        restored = Manager(self.manager.directory,self.client)
        self.assertEqual(restored.get(key)['status'], 'interrupted')
        restored.control(key, 'resume')
        restored.tick()
        self.assertEqual(restored.get(key)['session_id'],'ses_fixture')
        self.client.create.assert_called_once();self.client.prompt.assert_called_once()

    def test_restart_marks_assignment_interrupted_and_exposes_resume(self):
        key = self.create()
        self.manager.jobs[key]['status'] = 'running'
        self.manager.save(self.manager.jobs[key])
        restored = Manager(self.manager.directory, self.client)
        interrupted = restored.get(key)
        self.assertEqual(interrupted['status'], 'interrupted')
        self.assertTrue(interrupted['resume_session'])
        resumed = restored.control(key, 'resume')
        self.assertEqual(resumed['status'], 'queued')
        self.assertTrue(resumed['resume_session'])

    def test_interrupted_assignment_can_resume_without_existing_session(self):
        job = self.manager.create(self.values, 'admin')
        self.manager.jobs[job['id']].update(status='interrupted', session_id=None)
        resumed = self.manager.control(job['id'], 'resume')
        self.assertEqual(resumed['status'], 'queued')
        self.manager.tick()
        self.client.create.assert_called_once()
        self.client.prompt.assert_called_once()

    def test_pending_prompt_reuses_id_after_transport_failure(self):
        self.client.prompt.side_effect = [RuntimeError('temporary transport failure'),None]
        job = self.manager.create(self.values,'admin');self.manager.tick()
        first = self.client.prompt.call_args.args[2]
        restored=Manager(self.manager.directory,self.client)
        restored.control(job['id'], 'resume')
        restored.jobs[job['id']]['next_retry']=0;restored.tick()
        self.assertEqual(self.client.prompt.call_args.args[2],first)
        self.assertEqual(restored.get(job['id'])['cycles'],1)

    def test_questions_and_permissions_wait_for_real_answer(self):
        key=self.create();self.snap['active']=True
        self.snap['forms']=[{'id':'f1','fields':[]}]
        self.manager.tick();self.manager.tick()
        self.assertEqual(self.manager.get(key)['status'],'waiting_input')
        self.client.prompt.assert_called_once()
        self.manager.reply(key,'form','f1',{'choice':'API'})
        self.client.reply.assert_called_once_with('ses_fixture','form','f1',{'choice':'API'})
        self.assertEqual(self.manager.get(key)['status'],'running')
        with self.assertRaises(ValueError):self.manager.reply(key,'permission','unowned','once')

    def test_permission_can_be_allowed_for_one_call_or_always(self):
        key=self.create();job=self.manager.jobs[key]
        job.update(status='waiting_input',permissions=[{'id':'p1'}])
        self.manager.reply(key,'permission','p1','always')
        self.client.reply.assert_called_with('ses_fixture','permission','p1','always')
        self.assertEqual(self.manager.get(key)['status'],'running')

    def test_assignment_shell_mode_is_saved(self):
        job=self.manager.create({**self.values,'shell_mode':'auto'},'admin')
        self.assertTrue(job['auto_commands'])
        self.assertEqual(job['shell_mode'],'auto')

    def test_completion_requires_successful_independent_verification(self):
        key=self.create(verification_command='python -m unittest')
        self.client.message.return_value={'status':'exited','exit':1,'output':{'output':'FAILED'}}
        self.finish_turn(assistant('a1','Done\n'+MARKER))
        self.assertEqual(self.manager.get(key)['status'],'running')
        self.assertIn('failed independent verification',self.client.prompt.call_args.args[1])
        self.client.message.return_value={'status':'exited','exit':0,'output':{'output':'OK'}}
        self.finish_turn(assistant('a2','Repaired and verified\n'+MARKER))
        self.assertEqual(self.manager.get(key)['status'],'completed')
        self.assertEqual(self.manager.get(key)['verification']['exit'],0)

    def test_truncated_completion_marker_is_not_success(self):
        key=self.create()
        self.finish_turn(assistant('a1',MARKER,finish='length'))
        self.assertEqual(self.manager.get(key)['status'],'running')

    def test_agent_reported_completion_is_labelled(self):
        key=self.create();self.finish_turn(assistant('a1','Evidence\n'+MARKER))
        self.assertEqual(self.manager.get(key)['status'],'completed')
        self.assertIn('Agent-reported',self.manager.get(key)['completion'])

    def test_pause_cannot_falsely_acknowledge_failed_interrupt(self):
        key=self.create();self.client.interrupt.side_effect=RuntimeError('offline')
        self.manager.control(key,'pause')
        self.assertEqual(self.manager.get(key)['status'],'pausing')
        self.assertTrue(self.manager.busy())
        self.client.interrupt.side_effect=None
        self.manager.tick();self.assertEqual(self.manager.get(key)['status'],'paused')
        self.manager.tick();self.client.prompt.assert_called_once()
        self.manager.control(key,'resume');self.assertEqual(self.manager.get(key)['status'],'queued')

    def test_repeated_error_blocks_instead_of_looping(self):
        key=self.create()
        self.snap['messages']=[assistant('a1','',error={'message':'model unavailable'})]
        for _ in range(4):
            self.manager.jobs[key]['next_retry']=0;self.manager.tick()
        self.assertEqual(self.manager.get(key)['status'],'blocked')

    def test_repeated_idle_result_stops_after_three_cycles(self):
        key=self.create()
        for i in range(3):self.finish_turn(assistant('a'+str(i),'No progress'))
        self.assertEqual(self.manager.get(key)['status'],'blocked')

    def test_budget_blocks_and_resume_extends_it(self):
        key=self.create(max_cycles=1);self.finish_turn(assistant('a1','More work'))
        self.assertEqual(self.manager.get(key)['status'],'blocked')
        self.manager.control(key,'resume');self.assertEqual(self.manager.get(key)['max_cycles'],51)

    def test_verification_restarts_by_observing_existing_command(self):
        key=self.create(verification_command='pytest')
        self.client.message.return_value={'status':'running'}
        self.finish_turn(assistant('a1',MARKER));self.client.verify.assert_called_once()
        restored=Manager(self.manager.directory,self.client)
        restored.control(key, 'resume')
        self.client.message.return_value={'status':'exited','exit':0,'output':{'output':'passed'}}
        restored.tick();self.client.verify.assert_called_once()
        self.assertEqual(restored.get(key)['status'],'completed')

    def test_model_is_prepared_once_and_again_on_resume(self):
        prepare=Mock();self.manager.prepare_model=prepare
        key=self.create();prepare.assert_called_once()
        self.snap['active']=True;self.manager.tick();prepare.assert_called_once()
        self.manager.control(key,'pause');self.manager.control(key,'resume');self.manager.tick()
        self.assertEqual(prepare.call_count,2)

    def test_resuming_older_assignment_does_not_preempt_running_assignment(self):
        first=self.create();self.manager.control(first,'pause')
        second=self.manager.create({**self.values,'title':'Second'},'admin')['id']
        self.manager.tick();self.snap['active']=True
        self.manager.control(first,'resume');self.manager.tick()
        self.assertEqual(self.manager.get(first)['status'],'queued')
        self.assertEqual(self.manager.get(second)['status'],'running')
        self.assertEqual(self.client.prompt.call_count,2)

    def test_steering_is_durable_and_does_not_duplicate_session(self):
        key=self.create();self.snap['active']=True
        self.manager.steer(key,'Also add a CLI.')
        restored=Manager(self.manager.directory,self.client);restored.control(key, 'resume');restored.tick()
        self.assertIn('Also add a CLI.',self.client.prompt.call_args.args[1])
        self.assertEqual(restored.get(key)['steering'],[])
        self.client.create.assert_called_once()

    def test_project_path_is_fixed_and_created_automatically(self):
        target=Path(self.tmp.name)/'agents'/'project'
        job=self.manager.create(self.values,'admin')
        self.assertEqual(job['project'],str(target))
        self.assertTrue(target.is_dir())

    def test_api_cannot_override_server_managed_project_path(self):
        for field in ('project','create_project'):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError,'managed by the server'):
                self.manager.create({**self.values,field:'/tmp/elsewhere'},'admin')

    def test_projects_and_models_are_validated(self):
        for extra in ({'project':'relative'}, {'model':{'providerID':'unknown','id':'x'}}, {'max_cycles':0}):
            with self.assertRaises(ValueError):self.manager.create({**self.values,**extra},'admin')
        self.client.create.assert_not_called()

    def test_active_assignment_cannot_be_deleted_but_stopped_assignment_can(self):
        key=self.create()
        with self.assertRaisesRegex(ValueError,'Stop or pause'):
            self.manager.delete(key)
        self.assertIsNotNone(self.manager.get(key))
        self.manager.control(key,'stop')
        self.manager.delete(key)
        self.assertIsNone(self.manager.get(key))

    def test_completed_project_download_is_an_archive_of_project_files(self):
        key=self.create()
        project=Path(self.manager.get(key)['project'])
        (project/'result.txt').write_text('completed output')
        self.manager.jobs[key]['status']='completed'
        stream,name=self.manager.download(key)
        self.assertEqual(name,project.name)
        with tarfile.open(fileobj=io.BytesIO(stream.getvalue()),mode='r:gz') as archive:
            member=archive.getmember(project.name+'/result.txt')
            self.assertEqual(archive.extractfile(member).read(),b'completed output')

    def test_project_download_rejects_incomplete_assignment(self):
        key=self.create()
        with self.assertRaisesRegex(ValueError,'after the assignment completes'):
            self.manager.download(key)


class AgentAPITests(unittest.TestCase):
    def setUp(self):
        self.client=app.app.test_client()
        self.stack=__import__('contextlib').ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(app.users_store,'get_user_by_id',return_value={'id':'admin','username':'admin','role':'admin'}))
        self.manager=Mock();self.manager.busy.return_value=False
        self.stack.enter_context(patch.object(app,'_agent_tasks',self.manager))
        self.stack.enter_context(patch.object(app._model_test_registry,'list',return_value=[]))

    def login(self):
        with self.client.session_transaction() as s:
            s.update(logged_in=True,user_id='admin',role='admin',username='admin',last_activity=time.time(),login_csrf_token='test')

    def test_auth_and_csrf_protect_real_execution(self):
        self.assertEqual(self.client.get('/api/agent-tasks').status_code,401)
        self.login()
        self.assertEqual(self.client.post('/api/agent-tasks',json={}).status_code,400)
        self.manager.create.assert_not_called()
        with patch.object(app.users_store,'get_user_by_id',return_value={'id':'admin','username':'admin','role':'user'}):
            self.assertEqual(self.client.get('/api/agent-tasks').status_code,403)

    def test_agent_models_marks_currently_loaded_model(self):
        self.login()
        self.manager.client.models.return_value=[{**MODEL,'name':'Qwen'}]
        inventory={'models':[{'id':MODEL['id'],'backend':MODEL['providerID'],'state':'loaded'}]}
        with patch.object(app.api_list_models,'__wrapped__',return_value=Mock(get_json=lambda:inventory)):
            response=self.client.get('/api/agent-tasks/models')
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json['models'][0]['loaded'])

    def test_start_is_blocked_during_model_validation(self):
        self.login()
        with patch.object(app._model_test_registry,'list',return_value=[{'status':'running'}]):
            r=self.client.post('/api/agent-tasks',json={},headers={'X-CSRF-Token':'test'})
        self.assertEqual(r.status_code,409);self.manager.create.assert_not_called()

    def test_active_agent_prevents_manual_model_switch_and_tests(self):
        self.login();self.manager.busy.return_value=True
        for path in ('/api/models/load','/api/models/unload','/api/models/delete','/api/model-tests'):
            r=self.client.post(path,json={},headers={'X-CSRF-Token':'test'})
            self.assertEqual(r.status_code,409,path)

    def test_healthy_already_serving_model_does_not_reload_optional_backends(self):
        with patch.object(app.fastflowlm,'requests_paused',return_value=False), patch.object(app.fastflowlm,'current_served_model',return_value=MODEL['id']), patch.object(app.requests,'get',return_value=Mock()) as get, patch.object(app,'_test_load_model') as load:
            app._prepare_agent_model({'model':MODEL})
        load.assert_not_called();get.assert_called_once()

    def test_explicit_resume_loads_a_manually_unloaded_model(self):
        with patch.object(app.fastflowlm,'requests_paused',return_value=True), patch.object(app,'_test_load_model') as load:
            app._prepare_agent_model({'model':MODEL})
        load.assert_called_once_with({'backend':MODEL['providerID'],'id':MODEL['id']})

    def test_startup_preserves_agent_backend(self):
        self.manager.busy.return_value=True
        with patch.object(app,'_restore_default_model') as restore:
            app._warm_up_startup_models();restore.assert_not_called()
