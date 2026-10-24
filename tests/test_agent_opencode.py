import unittest
from unittest.mock import Mock, patch
import requests
from agent_opencode import OpenCode
from agent_container import ContainerOpenCode, _system_ca_bundle
import tempfile


class OpenCodeAdapterTests(unittest.TestCase):
    def test_session_uses_opencode_v2_shell_and_subagent_permissions(self):
        client=OpenCode();job={'title':'Test','model':{'providerID':'lmstudio','id':'model'},'project':'/tmp/project','auto_commands':False}
        with patch.object(client,'call',return_value={'data':{'id':'session'}}) as call:
            client.create(job)
        payload=call.call_args.args[2]
        rules=payload['permissions']
        self.assertIn({'action':'shell','resource':'*','effect':'ask'},rules)
        self.assertIn({'action':'subagent','resource':'*','effect':'deny'},rules)
        self.assertFalse(any(rule['action'] in ('bash','task') for rule in rules))

    def test_auto_command_mode_allows_v2_shell_action(self):
        client=OpenCode();job={'title':'Test','model':{'providerID':'lmstudio','id':'model'},'project':'/tmp/project','auto_commands':True}
        with patch.object(client,'call',return_value={'data':{'id':'session'}}) as call:
            client.create(job)
        self.assertIn({'action':'shell','resource':'*','effect':'allow'},call.call_args.args[2]['permissions'])

    def test_verification_observes_process_after_http_waiter_disconnects(self):
        client=OpenCode();calls=[]
        def call(method,path,data=None,params=None,**kwargs):
            calls.append((method,path,params))
            if '/message/' in path:return {'data':{'type':'shell','status':'running','shellID':'sh_owned'}}
            if path=='/api/session/ses_owned':return {'data':{'location':{'directory':'/tmp/project'}}}
            self.assertEqual(params.get('location[directory]'),'/tmp/project')
            if path.endswith('/output'):return {'data':{'output':'passed','size':6,'cursor':6,'truncated':False}}
            return {'data':{'status':'exited','exit':0}}
        with patch.object(client,'call',side_effect=call):
            result=client.message('ses_owned','msg_owned')
        self.assertEqual(result['status'],'exited');self.assertEqual(result['exit'],0)
        self.assertEqual(result['output']['output'],'passed')

    def test_verify_admission_does_not_block_on_long_execution(self):
        client=OpenCode()
        with patch.object(client,'call',side_effect=requests.ReadTimeout()) as call, patch.object(client,'message',return_value={'status':'running'}) as message:
            client.verify('ses_owned','pytest','msg_owned')
        self.assertEqual(call.call_args.kwargs['read_timeout'],1)
        message.assert_called_once_with('ses_owned','msg_owned')

    def test_verification_interruption_uses_owning_project_scope(self):
        client=OpenCode()
        with patch.object(client,'message',return_value={'status':'running','shellID':'sh_owned'}), patch.object(client,'call',return_value={'data':{'location':{'directory':'/tmp/owned'}}}) as call:
            client.stop_verification('ses_owned','msg_owned')
        self.assertEqual(call.call_args.args,('DELETE','/api/shell/sh_owned'))
        self.assertEqual(call.call_args.kwargs['params'],{'location[directory]':'/tmp/owned'})

    def test_small_context_models_are_visible_but_not_assignable(self):
        client=OpenCode()
        with patch.object(client,'call',return_value={'data':[{'providerID':'fastflowlm','id':'flm/qwen3.8-mtp:27b','name':'Qwen'}]}), patch('agent_opencode.flm_context.context_limit',return_value=8192):
            model=client.models()[0]
        self.assertFalse(model['usable']);self.assertEqual(model['context'],8192)


class ContainerRecoveryTests(unittest.TestCase):
    def test_runtime_migration_preserves_container_for_persisted_assignment(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ContainerOpenCode(directory)
            task_id = 'a' * 32
            task_dir = client.runtime_directory / task_id
            task_dir.mkdir()
            with patch('agent_container.agent_store.get_task', return_value={'id': task_id, 'status': 'interrupted'}), \
                 patch('agent_container.agent_store.get_runtime', return_value={'container': 'hearth-agent-' + task_id[:24]}), \
                 patch.object(client, '_docker') as docker:
                client.migrate_legacy_runtime()
            self.assertTrue(task_dir.exists())
            docker.assert_not_called()

    def test_runtime_migration_removes_directory_without_durable_task(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ContainerOpenCode(directory)
            task_id = 'b' * 32
            task_dir = client.runtime_directory / task_id
            task_dir.mkdir()
            with patch('agent_container.agent_store.get_task', return_value=None), \
                 patch('agent_container.agent_store.get_runtime', return_value=None), \
                 patch.object(client, '_docker') as docker:
                client.migrate_legacy_runtime()
            self.assertFalse(task_dir.exists())
            docker.assert_called_once_with('rm', '--force', 'hearth-agent-' + task_id[:24], timeout=60)

    def test_legacy_model_relay_metadata_uses_host_ca_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ContainerOpenCode(directory)
            name = 'hearth-agent-'+'a'*24
            task_id = 'a'*32
            import agent_store
            user = __import__('users_store').create_user('runtime-test-' + task_id, 'test-password', 'admin')
            self.addCleanup(__import__('users_store').delete_user, user['id'])
            agent_store.save_task({'id':task_id,'owner':user['id'],'updated_at':0})
            client._write_runtime(name, {'task_id':task_id,'container':name,'model_proxy':{'verify_tls':True}})
            info = client._read_runtime(name)
            self.assertEqual(info['model_proxy']['verify_tls'], _system_ca_bundle())

    def test_missing_container_recreates_session_from_project_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ContainerOpenCode(directory)
            job = {'id':'a'*32,'session_id':'hearth-agent-'+'a'*24+'::old-session'}
            with patch.object(client,'snapshot',side_effect=RuntimeError('The isolated agent container is missing.')) as snapshot, \
                 patch.object(client,'cleanup') as cleanup, \
                 patch.object(client,'create',return_value='hearth-agent-'+'a'*24+'::new-session') as create, \
                 patch.object(client,'_report'):
                result=client.resume(job)
            snapshot.assert_called_once_with(job['session_id'])
            cleanup.assert_called_once_with(job)
            create.assert_called_once_with(job)
            self.assertTrue(result.endswith('::new-session'))
