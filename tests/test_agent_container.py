"""Regressions for isolated agent container recovery."""
import tempfile
import http.client
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from agent_container import ContainerOpenCode, _ModelProxy


class AgentContainerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.client = ContainerOpenCode(Path(self.tempdir.name) / 'runtime')

    def test_lowercase_docker_missing_object_is_reported_as_missing_container(self):
        with patch.object(self.client, '_make_proxy'), patch.object(
            self.client, '_docker', side_effect=RuntimeError(
                'Docker agent runtime failed: error: no such object: hearth-agent-fixture')):
            with self.assertRaisesRegex(RuntimeError, 'isolated agent container is missing'):
                self.client._endpoint('hearth-agent-fixture', {'model_proxy': {}})

    def test_other_docker_inspect_errors_are_preserved(self):
        with patch.object(self.client, '_make_proxy'), patch.object(
            self.client, '_docker', side_effect=RuntimeError('Docker daemon is unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'Docker daemon is unavailable'):
                self.client._endpoint('hearth-agent-fixture', {'model_proxy': {}})

    def test_new_container_uses_assignment_thinking_choice(self):
        job = {'id': 'c'*32, 'owner': 'fixture', 'title': 'Check', 'enable_thinking': False,
               'model': {'providerID': 'lmstudio', 'id': 'fixture-model'}}
        provider = {'options': {}, 'models': {'fixture-model': {'limit': {'context': 16384}}}}
        proxy = Mock(info={'socket_path': '/fixture/model.sock'})
        with patch.object(self.client, '_ensure_image'), \
             patch.object(self.client, '_host_provider', return_value=({'enable_thinking': True}, provider, {})), \
             patch('agent_container.agent_store.get_runtime', return_value=None), \
             patch.object(self.client, '_make_proxy', return_value=proxy), \
             patch.object(self.client, '_write_runtime'), \
             patch.object(self.client, '_start_container',
                          side_effect=lambda name, info, *args: info.update(port=4096)) as start, \
             patch('agent_container._EndpointOpenCode') as endpoint, \
             patch.object(self.client, '_sync_to_container'):
            endpoint.return_value.call.return_value = {'data': {'id': 'new-session'}}
            self.client.create(job)
        config = start.call_args.args[1]['opencode_config']
        self.assertFalse(config['provider']['lmstudio']['options']['extraBody']['chat_template_kwargs']['enable_thinking'])
        self.assertFalse(config['provider']['lmstudio']['options']['extraBody']['enable_thinking'])

    def test_resume_replaces_session_with_stale_thinking_setting(self):
        job = {'id': 'c'*32, 'model': {'providerID': 'lmstudio', 'id': 'fixture'},
               'enable_thinking': False, 'session_id': 'hearth-agent-'+'c'*24+'::old'}
        info = {'tool_policy_version': 'native-tools-v1', 'opencode_config': {'provider': {
            'lmstudio': {'options': {'extraBody': {'chat_template_kwargs': {'enable_thinking': True}}}}}}}
        with patch.object(self.client, '_read_runtime', return_value=info), \
             patch.object(self.client, 'create', return_value='fresh') as create, \
             patch.object(self.client, 'snapshot') as snapshot:
            self.assertEqual(self.client.resume(job), 'fresh')
        create.assert_called_once_with(job)
        snapshot.assert_not_called()

    def test_resume_refreshes_lmstudio_session_missing_native_thinking_flag(self):
        job = {'id': 'c'*32, 'model': {'providerID': 'lmstudio', 'id': 'fixture'},
               'enable_thinking': True, 'session_id': 'hearth-agent-'+'c'*24+'::old'}
        info = {'tool_policy_version': 'native-tools-v1', 'opencode_config': {'provider': {
            'lmstudio': {'options': {'extraBody': {'chat_template_kwargs': {'enable_thinking': True}}}}}}}
        with patch.object(self.client, '_read_runtime', return_value=info), \
             patch.object(self.client, 'create', return_value='fresh') as create:
            self.assertEqual(self.client.resume(job), 'fresh')
        create.assert_called_once_with(job)

    def test_relay_forwards_small_sse_tool_deltas_before_upstream_finishes(self):
        release = threading.Event()
        progressed = threading.Event()
        line = b'data: {"choices":[{"delta":{"tool_calls":[{"function":{"name":"read","arguments":"{}"}}]}}]}\r\n'

        class Upstream(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Connection', 'close')
                self.end_headers()
                self.wfile.write(line+b'\n')
                self.wfile.flush()
                release.wait(5)
                self.wfile.write(b'data: [DONE]\n\n')
                self.close_connection = True

        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        relay = _ModelProxy({'socket_path': str(Path(self.tempdir.name)/'model.sock'),
                             'token': 'fixture', 'task_id': 'task-fixture',
                             'base_url': f'http://127.0.0.1:{upstream.server_port}/v1'})
        connection = http.client.HTTPConnection('localhost', timeout=2)
        try:
            with patch('agent_container.live_requests.start', return_value=1) as start, \
                 patch('agent_container.live_requests.update', side_effect=lambda *args, **kwargs: progressed.set()), \
                 patch('agent_container.live_requests.finish'):
                relay.start()
                connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.sock.settimeout(2)
                connection.sock.connect(relay.info['socket_path'])
                connection.request('POST', '/v1/chat/completions', body='{"stream":true}',
                                   headers={'Authorization': 'Bearer fixture', 'Content-Type': 'application/json'})
                response = connection.getresponse()
                self.assertEqual(response.readline(), line)
                self.assertEqual(response.readline(), b'\n')
                self.assertTrue(progressed.wait(1), 'Tool argument deltas must count as model progress')
                self.assertEqual(start.call_args.kwargs['task_id'], 'task-fixture')
                release.set()
                response.read()
        finally:
            release.set()
            connection.close()
            relay.close()
            upstream.shutdown()
            upstream.server_close()
            thread.join(2)

    def test_interrupt_closes_only_its_active_agent_model_stream(self):
        started = threading.Event()
        closed = threading.Event()
        response = Mock(status_code=200, ok=True, headers={'Content-Type': 'text/event-stream'})

        def chunks(_size):
            started.set()
            closed.wait(2)
            if False:
                yield b''

        response.iter_content.side_effect = chunks
        response.close.side_effect = closed.set
        name = 'hearth-agent-fixture'
        relay = _ModelProxy({'socket_path': str(Path(self.tempdir.name)/'interrupt.sock'),
                             'token': 'fixture', 'task_id': 'task-fixture', 'provider_id': 'lmstudio',
                             'base_url': 'http://127.0.0.1:1234/v1'})
        connection = http.client.HTTPConnection('localhost', timeout=2)
        try:
            with patch('agent_container.requests.request', return_value=response), \
                 patch('agent_container.live_requests.start', return_value=1), \
                 patch('agent_container.live_requests.finish'), \
                 patch.object(self.client, '_split', return_value=(name, 'session', {})), \
                 patch.object(self.client, '_endpoint') as endpoint:
                relay.start()
                self.client._proxies[name] = relay
                connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.sock.settimeout(2)
                connection.sock.connect(relay.info['socket_path'])
                connection.request('POST', '/v1/chat/completions', body='{"stream":true}',
                                   headers={'Authorization': 'Bearer fixture', 'Content-Type': 'application/json'})
                self.assertEqual(connection.getresponse().status, 200)
                self.assertTrue(started.wait(1))
                self.client.interrupt(name + '::session')
                endpoint.return_value.interrupt.assert_called_once_with('session')
                self.assertTrue(closed.wait(1), 'Interrupt must close the active upstream response')
        finally:
            connection.close()
            relay.close()


if __name__ == '__main__':
    unittest.main()
