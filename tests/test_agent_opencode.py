import unittest
import copy
import hashlib
from unittest.mock import Mock, patch
import requests
import socket
from hearth.agents.agent_opencode import OpenCode
from hearth.agents import agent_container
from hearth.agents.agent_container import ContainerOpenCode, _ModelProxy, _system_ca_bundle
import tempfile
from pathlib import Path


class OpenCodeAdapterTests(unittest.TestCase):
    def test_container_permissions_allow_project_and_auto_mode_without_approval(self):
        client=ContainerOpenCode('/tmp/hearth-agent-test')
        rules=client._permissions({'auto_commands':True})
        self.assertEqual(rules[0],{'action':'external_directory','resource':'/home/node/agents/project/*','effect':'allow'})
        self.assertEqual(rules[1],{'action':'*','resource':'*','effect':'allow'})
        self.assertEqual(rules[2],{'action':'shell','resource':'*','effect':'allow'})
        self.assertEqual(rules[-3],{'action':'external_directory','resource':'*','effect':'deny'})
        self.assertEqual(rules[-2],{'action':'subagent','resource':'*','effect':'deny'})
        self.assertEqual(rules[-1],{'action':'execute','resource':'*','effect':'deny'})

    def test_container_permissions_ask_only_for_shell_when_requested(self):
        client=ContainerOpenCode('/tmp/hearth-agent-test')
        rules=client._permissions({'auto_commands':False})
        self.assertEqual(rules[2],{'action':'shell','resource':'*','effect':'ask'})

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

    def test_permission_reply_encodes_opaque_request_id(self):
        client=OpenCode()
        with patch.object(client,'call') as call:
            client.reply('ses_owned','permission','web/search?confirm=1','once')
        self.assertEqual(call.call_args.args[1],
                         '/api/session/ses_owned/permission/web%2Fsearch%3Fconfirm%3D1/reply')
        self.assertEqual(call.call_args.args[2],{'decision':'once'})

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
        with patch.object(client,'call',return_value={'data':[{'providerID':'fastflowlm','id':'flm/qwen3.8-mtp:27b','name':'Qwen'}]}), patch('hearth.agents.agent_opencode.flm_context.context_limit',return_value=8192):
            model=client.models()[0]
        self.assertFalse(model['usable']);self.assertEqual(model['context'],8192)

    def test_opencode_console_models_are_valid_agent_models(self):
        """A hosted opencode.ai provider in opencode.jsonc must appear in the agent model
        picker with its own advertised context window."""
        client = OpenCode()
        data = {'data': [{'providerID': 'opencode', 'id': 'glm-5.3', 'name': 'GLM 5.3',
                          'limit': {'context': 200000}}]}
        with patch.object(client, 'call', return_value=data):
            models = client.models()
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]['providerID'], 'opencode')
        self.assertTrue(models[0]['usable'])
        self.assertEqual(models[0]['context'], 200000)


class ContainerRecoveryTests(unittest.TestCase):
    def test_hosted_picker_replaces_stale_desktop_catalog(self):
        client = ContainerOpenCode('/tmp/hearth-agent-test')
        stale = [{'providerID': 'opencode', 'id': 'retired-free'},
                 {'providerID': 'lmstudio', 'id': 'local-model'}]
        config = {'provider': {'opencode': {'models': {
            'current-model': {'limit': {'context': 65536}},
        }}}}
        with patch.object(OpenCode, 'models', return_value=stale), \
             patch('hearth.agents.agent_container._generated_model_config', return_value=config):
            models = client.models()
        self.assertEqual([(m['providerID'], m['id']) for m in models],
                         [('lmstudio', 'local-model'), ('opencode', 'current-model')])

    def test_hosted_relay_uses_current_plan_and_key(self):
        client = ContainerOpenCode('/tmp/hearth-agent-test')
        old = {'provider': {'opencode': {'models': {'model': {}}, 'options': {
            'baseURL': 'https://example.com/previous/v1', 'apiKey': 'old-key',
        }}}}
        current = copy.deepcopy(old)
        current['provider']['opencode']['options'] = {
            'baseURL': 'https://example.com/current/v1', 'apiKey': 'current-key',
        }
        with patch('hearth.agents.agent_container.parse_jsonc', return_value=old), \
             patch('hearth.agents.agent_container._generated_model_config', return_value=current):
            _, _, relay = client._host_provider('opencode', 'model', '/tmp/test.sock', 'Test')
        self.assertEqual(relay['base_url'], 'https://example.com/current/v1')
        # Hosted credentials are resolved by the host proxy at request time.
        self.assertIsNone(relay['api_key'])

    def test_kobold_agent_relay_enables_tools_for_stale_model_metadata(self):
        client = ContainerOpenCode('/tmp/hearth-agent-test')
        config = {'provider': {'kobold': {
            'models': {'kobold-model': {'limit': {'context': 8192},
                                        'capabilities': {'tools': False}}},
            'options': {'baseURL': 'https://kobold.example/v1', 'apiKey': 'test-key'},
        }}}
        with patch('hearth.agents.agent_container.parse_jsonc', return_value=config), \
             patch('hearth.agents.agent_container._generated_model_config', return_value=None):
            _, provider, _ = client._host_provider(
                'kobold', 'kobold-model', '/tmp/model.sock', 'Test agent')
        self.assertTrue(provider['models']['kobold-model']['capabilities']['tools'])
        self.assertFalse(config['provider']['kobold']['models']['kobold-model']['capabilities']['tools'])

    def test_resume_restores_durable_workspace_to_existing_container(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ContainerOpenCode(directory)
            task_id = 'c' * 32
            name = 'hearth-agent-' + task_id[:24]
            job = {'id': task_id, 'owner': 'owner-1',
                   'model': {'providerID': 'lmstudio', 'id': 'model'},
                   'session_id': name + '::saved-session'}
            empty_skill_hash = hashlib.sha256(b'{}').hexdigest()
            runtime = {'tool_policy_version': 'native-tools-v2',
                       'model_proxy': {'skill_bundle_hash': empty_skill_hash,
                                       'agent_prompt_version': agent_container.AGENT_PROMPT_VERSION,
                                       'research_providers_hash': '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945',
                                       'research_credentials_hash': '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a'},
                       'opencode_config': {'provider': {'lmstudio': {'options': {
                           'extraBody': {'enable_thinking': True,
                                         'chat_template_kwargs': {'enable_thinking': True}}}}}}}
            with patch.object(client, '_read_runtime',
                              return_value=runtime), \
                 patch.object(client, '_skill_bundle', return_value={}), \
                 patch.object(client, 'snapshot', return_value={'messages': []}), \
                 patch.object(client, '_sync_to_container') as restore:
                self.assertEqual(client.resume(job), job['session_id'])
            restore.assert_called_once_with(name, 'owner-1', workspace_id=task_id)

    def test_resume_rebuilds_container_when_selected_skill_content_changes(self):
        client = ContainerOpenCode('/tmp/hearth-agent-test')
        job = {'id': 'c'*32, 'model': {'providerID': 'lmstudio', 'id': 'fixture'},
               'enable_thinking': True, 'session_id': 'hearth-agent-'+'c'*24+'::old'}
        info = {'tool_policy_version': 'native-tools-v2',
                'model_proxy': {'skill_bundle_hash': 'old-skill-hash',
                                'agent_prompt_version': agent_container.AGENT_PROMPT_VERSION,
                                'research_providers_hash': '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945',
                                'research_credentials_hash': '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a'},
                'opencode_config': {'provider': {'lmstudio': {'options': {'extraBody': {
                    'enable_thinking': True,
                    'chat_template_kwargs': {'enable_thinking': True}}}}}}}
        with patch.object(client, '_read_runtime', return_value=info), \
             patch.object(client, '_skill_bundle', return_value={'deploy-helper': 'updated body'}), \
             patch.object(client, 'create', return_value='fresh') as create, \
             patch.object(client, 'snapshot') as snapshot:
            self.assertEqual(client.resume(job), 'fresh')
        create.assert_called_once_with(job)
        snapshot.assert_not_called()

    def test_endpoint_refreshes_container_host_port(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ContainerOpenCode(directory)
            info = {'password': 'secret', 'port': 43000, 'model_proxy': {}}
            with patch.object(client, '_make_proxy'), \
                 patch.object(client, '_docker', side_effect=['true', '127.0.0.1:43001']), \
                 patch.object(client, '_write_runtime') as write_runtime:
                endpoint = client._endpoint('hearth-agent-' + 'a' * 24, info)
            self.assertEqual(endpoint.endpoint, 'http://127.0.0.1:43001')
            self.assertEqual(info['port'], 43001)
            write_runtime.assert_called_once()

    def test_agent_config_controls_thinking_and_preserves_template_options(self):
        client = ContainerOpenCode('/tmp/hearth-agent-test')
        provider = {'options': {'extraBody': {'chat_template_kwargs': {'temperature': 0.2}, 'keep': 'yes'}}}
        enabled = client._agent_config({'enable_thinking': True}, 'lemonade', 'model', copy.deepcopy(provider))
        disabled = client._agent_config({'enable_thinking': False}, 'lemonade', 'model', copy.deepcopy(provider))
        self.assertTrue(enabled['provider']['lemonade']['options']['extraBody']['chat_template_kwargs']['enable_thinking'])
        self.assertFalse(disabled['provider']['lemonade']['options']['extraBody']['chat_template_kwargs']['enable_thinking'])
        self.assertEqual(disabled['provider']['lemonade']['options']['extraBody']['chat_template_kwargs']['temperature'], 0.2)
        self.assertEqual(disabled['provider']['lemonade']['options']['extraBody']['keep'], 'yes')

    def test_opencode_agent_config_omits_unsupported_template_kwargs(self):
        client = ContainerOpenCode('/tmp/hearth-agent-config-test')
        provider = {'options': {'extraBody': {
            'chat_template_kwargs': {'temperature': 0.2}, 'keep': 'yes'}}}
        config = client._agent_config({'enable_thinking': True}, 'opencode', 'beam-free', provider)
        extra = config['provider']['hearth-opencode']['options']['extraBody']
        self.assertNotIn('chat_template_kwargs', extra)
        self.assertEqual(extra['keep'], 'yes')

    def test_agent_config_enables_compaction_by_default(self):
        client = ContainerOpenCode('/tmp/hearth-agent-test')
        provider = {'options': {}, 'models': {
            'model': {'limit': {'context': 32768, 'output': 8192}}}}
        config = client._agent_config({}, 'lemonade', 'model', provider)
        self.assertTrue(config['compaction']['auto'])

    def test_container_reuse_check_accepts_proxy_info(self):
        existing = {'tool_policy_version': agent_container.TOOL_POLICY_VERSION,
                    'model': {'providerID': 'lemonade', 'id': 'model'},
                    'model_proxy': {'base_url': 'http://127.0.0.1:13305/v1',
                                    'api_key': 'key', 'headers': {}, 'verify_tls': True},
                    'opencode_config': {'provider': {'lemonade': {'options': {'apiKey': 'old'}}}}}
        job = {'model': {'providerID': 'lemonade', 'id': 'model'}}
        config = {'provider': {'lemonade': {'options': {'apiKey': 'new'}}}}
        proxy_info = {'base_url': 'http://127.0.0.1:13305/v1',
                      'api_key': 'key', 'headers': {}, 'verify_tls': True}
        self.assertTrue(ContainerOpenCode._can_reuse_container(existing, job, config, proxy_info))

    def test_agent_config_bounds_compaction_to_selected_model_context(self):
        client = ContainerOpenCode('/tmp/hearth-agent-test')
        provider = {'options': {}, 'models': {'model': {'limit': {'context': 65536, 'output': 8192}}}}
        config = client._agent_config(
            {'compaction': {'auto': True, 'keep': {'tokens': 65536}, 'buffer': 1024}},
            'lemonade', 'model', provider)
        self.assertEqual(config['compaction'], {
            'auto': True, 'keep': {'tokens': 40960}, 'buffer': 16384,
        })

    def test_agent_config_does_not_narrow_a_large_muse_window_when_smaller_siblings_exist(self):
        client = ContainerOpenCode('/tmp/hearth-agent-test')
        provider = {'options': {}, 'models': {
            'Muse-Glimmer-30B-GGUF': {'limit': {'context': 65536, 'output': 8192}},
            'Muse-Sibri-3.6B-GGUF': {'limit': {'context': 1024, 'output': 1024}},
            'GLM-4.7-Flash-GGUF': {'limit': {'context': 4096, 'output': 2048}},
        }}
        config = client._agent_config(
            {'compaction': {'auto': True, 'keep': {'tokens': 1024}, 'buffer': 1024}},
            'lemonade', 'Muse-Glimmer-30B-GGUF', provider)
        self.assertEqual(config['compaction'], {
            'auto': True, 'keep': {'tokens': 40960}, 'buffer': 16384,
        })

    def test_agent_config_scales_compaction_for_a_muse_style_lemonade_window(self):
        client = ContainerOpenCode('/tmp/hearth-agent-test')
        for model, context, expected_keep, expected_buffer in (
            ('Muse-Glimmer-30B-GGUF', 65536, 40960, 16384),
            ('Muse-Sibri-3.6B-GGUF', 131072, 65536, 32768),
            ('GLM-4.7-Flash-GGUF', 32768, 4096, 4096),
            ('qwen3.8-2b-distill', 8192, 1024, 1024),
            ('Ornith-1.5-9B-GGUF', 16384, 2048, 2048),
        ):
            with self.subTest(model=model):
                provider = {'options': {}, 'models': {
                    model: {'limit': {'context': context, 'output': 8192}}}}
                config = client._agent_config({}, 'lemonade', model, provider)
                self.assertEqual(config['compaction']['keep']['tokens'], expected_keep)
                self.assertEqual(config['compaction']['buffer'], expected_buffer)

    def test_agent_config_caps_an_oversized_compaction_buffer_to_the_model_window(self):
        client = ContainerOpenCode('/tmp/hearth-agent-test')
        provider = {'options': {}, 'models': {
            'Muse-Glimmer-30B-GGUF': {'limit': {'context': 65536, 'output': 8192}}}}
        config = client._agent_config(
            {'compaction': {'auto': True, 'keep': {'tokens': 65536}, 'buffer': 32768}},
            'lemonade', 'Muse-Glimmer-30B-GGUF', provider)
        self.assertEqual(config['compaction'], {
            'auto': True, 'keep': {'tokens': 40960}, 'buffer': 16384,
        })

    def test_live_model_relay_socket_is_reused_instead_of_unlinked(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'relay.sock'
            owner=_ModelProxy({'socket_path':str(path),'token':'test-token'})
            duplicate=_ModelProxy({'socket_path':str(path),'token':'test-token'})
            try:
                owner.start()
                original_inode=path.stat().st_ino
                duplicate.start()
                self.assertEqual(path.stat().st_ino,original_inode)
                with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as probe:
                    probe.settimeout(2)
                    probe.connect(str(path))
                    probe.sendall(b'GET /health HTTP/1.1\r\nHost: local\r\n\r\n')
                    self.assertIn(b'404',probe.recv(1024))
            finally:
                owner.close()

    def test_runtime_migration_preserves_container_for_persisted_assignment(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ContainerOpenCode(directory)
            task_id = 'a' * 32
            task_dir = client.runtime_directory / task_id
            task_dir.mkdir()
            with patch('hearth.agents.agent_container.agent_store.get_task', return_value={'id': task_id, 'status': 'interrupted'}), \
                 patch('hearth.agents.agent_container.agent_store.get_runtime', return_value={'container': 'hearth-agent-' + task_id[:24]}), \
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
            with patch('hearth.agents.agent_container.agent_store.get_task', return_value=None), \
                 patch('hearth.agents.agent_container.agent_store.get_runtime', return_value=None), \
                 patch.object(client, '_docker') as docker:
                client.migrate_legacy_runtime()
            self.assertFalse(task_dir.exists())
            docker.assert_called_once_with('rm', '--force', 'hearth-agent-' + task_id[:24], timeout=60)

    def test_legacy_model_relay_metadata_uses_host_ca_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ContainerOpenCode(directory)
            name = 'hearth-agent-'+'a'*24
            task_id = 'a'*32
            from hearth.agents import agent_store
            from hearth.storage import users_store
            user = users_store.create_user('runtime-test-' + task_id, 'test-password', 'admin')
            self.addCleanup(users_store.delete_user, user['id'])
            agent_store.save_task({'id':task_id,'owner':user['id'],'updated_at':0})
            client._write_runtime(name, {'task_id':task_id,'container':name,'model_proxy':{'verify_tls':True}})
            info = client._read_runtime(name)
            self.assertEqual(info['model_proxy']['verify_tls'], _system_ca_bundle())

    def test_missing_container_recreates_session_from_project_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            client = ContainerOpenCode(directory)
            job = {'id':'a'*32, 'model': {'providerID': 'lmstudio', 'id': 'model'},
                   'session_id':'hearth-agent-'+'a'*24+'::old-session'}
            empty_skill_hash = hashlib.sha256(b'{}').hexdigest()
            runtime = {'tool_policy_version': 'native-tools-v2',
                       'model_proxy': {'skill_bundle_hash': empty_skill_hash,
                                       'agent_prompt_version': agent_container.AGENT_PROMPT_VERSION,
                                       'research_providers_hash': '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945',
                                       'research_credentials_hash': '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a'},
                       'opencode_config': {'provider': {'lmstudio': {'options': {
                           'extraBody': {'enable_thinking': True,
                                         'chat_template_kwargs': {'enable_thinking': True}}}}}}}
            with patch.object(client,'_read_runtime', return_value={
                    **runtime}), \
                 patch.object(client, '_skill_bundle', return_value={}), \
                 patch.object(client,'snapshot',side_effect=RuntimeError('The isolated agent container is missing.')) as snapshot, \
                 patch.object(client,'cleanup') as cleanup, \
                 patch.object(client,'create',return_value='hearth-agent-'+'a'*24+'::new-session') as create, \
                 patch.object(client,'_report'):
                result=client.resume(job)
            snapshot.assert_called_once_with(job['session_id'])
            cleanup.assert_called_once_with(job)
            create.assert_called_once_with(job)
            self.assertTrue(result.endswith('::new-session'))
