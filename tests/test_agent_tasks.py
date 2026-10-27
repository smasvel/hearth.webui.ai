import copy
import io
import tarfile
import tempfile
import time
import uuid
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
        self.owner = app.users_store.create_user('agent-test-' + uuid.uuid4().hex, 'test-password', 'admin')['id']
        self.addCleanup(app.users_store.delete_user, self.owner)
        self.stack=__import__('contextlib').ExitStack()
        self.stack.enter_context(patch('agent_tasks.Path.home', return_value=Path(self.tmp.name)))
        self.addCleanup(self.stack.close)
        self.values = {'title':'Build project','goal':'Build and verify the feature','model':MODEL}

    def create(self, **extra):
        job = self.manager.create({**self.values, **extra}, self.owner)
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

    def test_thinking_choice_is_saved_and_defaults_on(self):
        enabled = self.manager.create(self.values, self.owner)
        disabled = self.manager.create({**self.values, 'enable_thinking': False}, self.owner)
        self.assertTrue(enabled['enable_thinking'])
        self.assertFalse(disabled['enable_thinking'])

    def test_thinking_choice_must_be_boolean(self):
        with self.assertRaisesRegex(ValueError, 'model thinking'):
            self.manager.create({**self.values, 'enable_thinking': 'false'}, self.owner)

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

    def test_maximum_steps_response_is_blocked_and_resumable_even_if_session_active(self):
        key = self.create()
        self.snap['active'] = True
        self.snap['messages'] = [assistant('max-steps', 'CRITICAL - MAXIMUM STEPS REACHED')]
        self.manager.tick()
        blocked = self.manager.get(key)
        self.assertEqual(blocked['status'], 'blocked')
        self.assertEqual(blocked['handled_message'], 'max-steps')
        resumed = self.manager.control(key, 'resume')
        self.assertEqual(resumed['status'], 'queued')
        self.assertEqual(resumed['max_steps_reviewed_message'], 'max-steps')
        self.manager.jobs[key]['model_prepared'] = True
        self.snap['active'] = False
        self.manager.tick()
        self.assertEqual(self.manager.get(key)['status'], 'running')
        self.assertIn('continue the assigned goal', self.client.prompt.call_args.args[1].lower())

    def test_interrupted_assignment_can_resume_without_existing_session(self):
        job = self.manager.create(self.values, self.owner)
        self.manager.jobs[job['id']].update(status='interrupted', session_id=None)
        resumed = self.manager.control(job['id'], 'resume')
        self.assertEqual(resumed['status'], 'queued')
        self.manager.tick()
        self.client.create.assert_called_once()
        self.client.prompt.assert_called_once()

    def test_pending_prompt_reuses_id_after_transport_failure(self):
        self.client.prompt.side_effect = [RuntimeError('temporary transport failure'),None]
        job = self.manager.create(self.values,self.owner);self.manager.tick()
        first = self.client.prompt.call_args.args[2]
        restored=Manager(self.manager.directory,self.client)
        self.assertEqual(restored.get(job['id'])['status'], 'queued')
        restored.jobs[job['id']]['next_retry']=0;restored.tick()
        self.assertEqual(self.client.prompt.call_args.args[2],first)
        self.assertEqual(restored.get(job['id'])['cycles'],1)

    def test_provider_connection_reset_retries_without_needs_review(self):
        key=self.create()
        self.client.snapshot.side_effect=RuntimeError(
            "{'type': 'provider.transport', 'message': 'ECONNRESET: The socket connection was closed unexpectedly'}")
        for _ in range(4):
            self.manager.jobs[key]['next_retry']=0
            self.manager.tick()
        current=self.manager.get(key)
        self.assertEqual(current['status'],'running')
        self.assertEqual(current['errors'],0)
        self.assertEqual(current['transport_retries'],4)
        self.assertIn('ECONNRESET',current['error'])

    def test_provider_error_result_retries_same_cycle_with_backoff(self):
        key=self.create()
        self.snap['messages']=[assistant('a_transport','',error={
            'type':'provider.transport','message':'ECONNRESET: socket closed'})]
        self.manager.tick();self.manager.tick()
        current=self.manager.get(key)
        self.assertEqual(current['status'],'running')
        self.assertIsNotNone(current['pending_prompt'])
        self.assertIn('retry the current assignment',current['pending_prompt']['text'].lower())
        self.assertEqual(current['cycles'],1)
        self.assertTrue(current['retry_current_cycle'])
        self.assertGreater(current['next_retry'],time.time())
        self.manager.jobs[key]['next_retry']=0
        self.manager.tick()
        retried=self.manager.get(key)
        self.assertEqual(retried['cycles'],1)
        self.assertFalse(retried['retry_current_cycle'])
        self.assertIn('retried after a provider connection reset',retried['activity'][-1]['text'].lower())

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
        job=self.manager.create({**self.values,'shell_mode':'auto'},self.owner)
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

    def test_completed_assignment_can_rerun_from_saved_task_state(self):
        key=self.create(enable_thinking=False)
        self.finish_turn(assistant('a1','Evidence\n'+MARKER))
        completed=self.manager.get(key)
        rerun=self.manager.rerun(key)
        self.assertEqual(rerun['status'],'queued')
        self.assertEqual(rerun['cycles'],0)
        self.assertEqual(rerun['run_count'],2)
        self.assertFalse(rerun['enable_thinking'])
        self.assertIsNone(rerun['completion'])
        self.assertTrue(any('Agent-reported completion' in item['text'] for item in rerun['activity']))
        self.assertIn('Rerun queued',rerun['activity'][-1]['text'])
        self.manager.tick()
        self.assertEqual(self.client.create.call_count,2)
        self.assertEqual(self.client.prompt.call_count,2)
        self.assertIn(self.values['goal'],self.client.prompt.call_args.args[1])
        self.assertEqual(self.manager.get(key)['status'],'running')

    def test_only_completed_assignments_can_rerun(self):
        key=self.create()
        with self.assertRaisesRegex(ValueError,'Only completed, stopped, interrupted, or blocked assignments'):
            self.manager.rerun(key)

    def test_missing_agent_container_is_queued_for_checkpoint_recovery(self):
        class MissingContainerClient:
            def ensure_agent(self): pass
            def models(self): return [{**MODEL, 'name': 'Fixture Qwen'}]
            def resume(self, _job): raise RuntimeError('The isolated agent container is missing.')

        self.manager.client = MissingContainerClient()
        key = self.manager.create(self.values, self.owner)['id']
        job = self.manager.jobs[key]
        job.update(status='queued', session_id='hearth-agent-fixture::ses_fixture',
                   resume_session=True, model_prepared=True, after_interrupt='blocked', errors=3)
        self.manager.save(job)
        self.manager.tick()
        recovered = self.manager.get(key)
        self.assertEqual(recovered['status'], 'queued')
        self.assertTrue(recovered['resume_session'])
        self.assertEqual(recovered['errors'], 0)
        self.assertNotIn('after_interrupt', recovered)
        self.assertIn('rebuilding it from the latest synchronized project checkpoint',
                      recovered['activity'][-1]['text'])

    def test_waiting_input_task_does_not_starve_a_queued_rerun(self):
        waiting=self.create()
        self.manager.jobs[waiting].update(status='waiting_input')
        self.manager.save(self.manager.jobs[waiting])
        completed=self.manager.create(self.values,self.owner)
        self.manager.jobs[completed['id']].update(status='completed',completion='Done')
        self.manager.save(self.manager.jobs[completed['id']])
        self.manager.rerun(completed['id'])
        self.manager.tick()
        self.assertEqual(self.manager.get(waiting)['status'],'waiting_input')
        self.assertEqual(self.manager.get(completed['id'])['status'],'running')
        self.assertEqual(self.client.create.call_count,2)

    def test_completion_marker_with_future_plan_or_placeholder_does_not_finish(self):
        key=self.create()
        self.finish_turn(assistant('a1','I will inspect the files and implement the project.\n'
                                   '[Complete full runnable solution]\n'+MARKER))
        current=self.manager.get(key)
        self.assertEqual(current['status'],'running')
        self.assertIsNone(current.get('completion'))
        self.assertEqual(self.client.prompt.call_count,2)
        self.assertIn('continue the assigned goal',self.client.prompt.call_args.args[1].lower())
        self.assertTrue(any('ignored a completion marker' in item['text'].lower()
                            for item in current['activity']))

    def test_pause_cannot_falsely_acknowledge_failed_interrupt(self):
        key=self.create();self.client.interrupt.side_effect=RuntimeError('offline')
        self.manager.control(key,'pause')
        self.assertEqual(self.manager.get(key)['status'],'interrupted')
        self.assertFalse(self.manager.busy())
        self.assertTrue(self.manager.get(key)['resume_session'])
        self.client.interrupt.side_effect=None
        self.manager.control(key,'resume')
        self.assertEqual(self.manager.get(key)['status'],'queued')

    def test_failed_stop_acknowledgement_leaves_assignment_resumable(self):
        key = self.create()
        self.client.interrupt.side_effect = RuntimeError('offline')
        stopped = self.manager.control(key, 'stop')
        self.assertEqual(stopped['status'], 'interrupted')
        self.assertTrue(stopped['resume_session'])
        self.manager.control(key, 'resume')
        self.assertEqual(self.manager.get(key)['status'], 'queued')

    def test_worker_interrupt_failure_blocks_with_continue_available(self):
        key = self.create()
        job = self.manager.jobs[key]
        job.update(status='stopping', after_interrupt='blocked', model_prepared=True)
        self.manager.save(job)
        self.client.interrupt.side_effect = RuntimeError('offline')
        self.manager.tick()
        blocked = self.manager.get(key)
        self.assertEqual(blocked['status'], 'blocked')
        self.assertTrue(blocked['resume_session'])
        self.assertNotIn('after_interrupt', blocked)

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

    def test_idle_wait_does_not_escalate_errors_or_change_selected_model(self):
        key=self.create();job=self.manager.jobs[key]
        job.update(awaiting_since=time.time()-200, errors=0)
        self.manager.save(job)
        self.manager.tick()
        current=self.manager.get(key)
        self.assertEqual(current['status'],'running')
        self.assertEqual(current['model'],MODEL)
        self.assertEqual(current['errors'],0)

    def test_long_idle_becomes_review_state_without_worker_error_retries(self):
        key=self.create();job=self.manager.jobs[key]
        job.update(awaiting_since=time.time()-1801, errors=0)
        self.manager.save(job)
        self.manager.tick()
        current=self.manager.get(key)
        self.assertEqual(current['status'],'blocked')
        self.assertIn('idle for 30 minutes',current['error'])
        self.assertEqual(current['errors'],0)
        self.assertEqual(current['model'],MODEL)

    def test_budget_blocks_and_resume_extends_it(self):
        key=self.create(max_cycles=1);self.finish_turn(assistant('a1','More work'))
        self.assertEqual(self.manager.get(key)['status'],'blocked')
        self.manager.control(key,'resume');self.assertEqual(self.manager.get(key)['max_cycles'],51)

    def test_human_review_can_reset_cycles_before_resuming(self):
        key=self.create(max_cycles=12)
        job=self.manager.jobs[key]
        job.update(status='blocked',cycles=12,errors=3,stalls=2,idle_polls=1,
                   next_retry=time.time()+60,fingerprint='repeat',error='review this result',
                   summary='Latest saved response')
        self.manager.save(job)
        reset=self.manager.control(key,'reset-cycles')
        self.assertEqual(reset['status'],'blocked')
        self.assertEqual(reset['cycles'],0)
        self.assertEqual(reset['max_cycles'],12)
        self.assertEqual(reset['errors'],0)
        self.assertEqual(reset['stalls'],0)
        self.assertEqual(reset['idle_polls'],0)
        self.assertEqual(reset['next_retry'],0)
        self.assertIsNone(reset['error'])
        self.assertEqual(reset['summary'],'Latest saved response')
        self.assertIn('reset after human review',reset['activity'][-1]['text'])
        resumed=self.manager.control(key,'resume')
        self.assertEqual(resumed['status'],'queued')
        self.assertEqual(resumed['cycles'],0)

    def test_cycle_reset_requires_reviewable_assignment(self):
        key=self.create()
        self.manager.jobs[key]['status']='queued'
        self.manager.save(self.manager.jobs[key])
        with self.assertRaisesRegex(ValueError,'while reviewing'):
            self.manager.control(key,'reset-cycles')

    def test_human_can_reset_cycles_while_assignment_is_running(self):
        key=self.create(max_cycles=20)
        reset=self.manager.control(key,'reset-cycles')
        self.assertEqual(reset['status'],'running')
        self.assertEqual(reset['cycles'],0)
        self.assertTrue(reset['started'])

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
        second=self.manager.create({**self.values,'title':'Second'},self.owner)['id']
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
        job=self.manager.create(self.values,self.owner)
        self.assertEqual(job['project'],'PostgreSQL private workspace')

    def test_api_cannot_override_server_managed_project_path(self):
        for field in ('project','create_project'):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError,'managed by the server'):
                self.manager.create({**self.values,field:'/tmp/elsewhere'},self.owner)

    def test_projects_and_models_are_validated(self):
        for extra in ({'project':'relative'}, {'model':{'providerID':'unknown','id':'x'}}, {'max_cycles':0}):
            with self.assertRaises(ValueError):self.manager.create({**self.values,**extra},self.owner)
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
        import agent_store
        agent_store.save_workspace_file(self.owner,'result.txt',b'completed output')
        self.manager.jobs[key]['status']='completed'
        stream,name=self.manager.download(key)
        self.assertEqual(name,'project')
        with tarfile.open(fileobj=io.BytesIO(stream.getvalue()),mode='r:gz') as archive:
            member=archive.getmember('project/result.txt')
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

    def test_agent_models_finds_loaded_coding_model_after_support_model(self):
        self.login()
        self.manager.client.models.return_value=[{**MODEL,'name':'Qwen'}]
        inventory={'models':[
            {'id':'text-embedding-nomic-embed-text-v1.5','backend':'lemonade','state':'loaded','type':'embeddings'},
            {'id':MODEL['id'],'backend':MODEL['providerID'],'state':'loaded','type':'llm'},
        ]}
        with patch.object(app.api_list_models,'__wrapped__',return_value=Mock(get_json=lambda:inventory)):
            response=self.client.get('/api/agent-tasks/models')
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json['models'][0]['loaded'])

    def test_agent_models_recognizes_lmstudio_loaded_instances(self):
        self.login()
        self.manager.client.models.return_value=[{**MODEL,'name':'Qwen'}]
        inventory={'models':[{'id':MODEL['id'],'backend':MODEL['providerID'],
                              'loaded_instances':[{'config':{'context_length':32768}}]}]}
        with patch.object(app.api_list_models,'__wrapped__',return_value=Mock(get_json=lambda:inventory)):
            response=self.client.get('/api/agent-tasks/models')
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json['models'][0]['loaded'])

    def test_live_agent_snapshot_includes_latest_result_and_completion(self):
        self.login()
        job = {
            'id': 'task-result', 'owner': 'admin', 'shared_with': [],
            'session_id': 'session-result', 'status': 'completed',
            'summary': 'The requested work is finished.',
            'completion': 'Verified by the assigned command.',
            'verification': {'passed': True, 'output': 'all checks passed'},
            'error': None, 'activity': [], 'messages': [],
        }
        self.manager.get.return_value = job
        response = self.client.get('/api/agent-tasks/task-result/live-messages')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['summary'], job['summary'])
        self.assertEqual(response.json['completion'], job['completion'])
        self.assertEqual(response.json['verification'], job['verification'])

    def test_live_agent_snapshot_falls_back_to_saved_messages_when_live_is_empty(self):
        self.login()
        saved_messages = [{
            'id': 'assistant-saved', 'type': 'assistant',
            'content': [{'type': 'text', 'text': 'The saved agent response.'}],
        }]
        job = {
            'id': 'task-saved-messages', 'owner': 'admin', 'shared_with': [],
            'session_id': 'session-saved-messages', 'status': 'waiting_input',
            'summary': '', 'activity': [], 'messages': saved_messages,
        }
        self.manager.get.return_value = job
        self.manager.client.live_messages.return_value = []

        response = self.client.get('/api/agent-tasks/task-saved-messages/live-messages')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['messages'], saved_messages)
        self.assertEqual(response.json['source'], 'saved')

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
