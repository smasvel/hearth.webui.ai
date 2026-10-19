import json
import unittest
from unittest.mock import patch

import flm_tool_shim as shim


BODY = {'tools': [{'type': 'function', 'function': {'name': name}}
                  for name in ('bash', 'write', 'read')]}


class BlockedToolTests(unittest.TestCase):
    def test_empty_runtime_call_finishes_as_text(self):
        event = {'choices': [{'delta': {'content': 'Fixed and verified.', 'tool_calls': [
            {'index': 0, 'id': 'c1', 'function': {'name': '', 'arguments': '{}'}}]},
            'finish_reason': 'tool_calls'}]}
        result = shim._guard_streamed_tool_calls([b'data: ' + json.dumps(event).encode()], BODY)
        choice = json.loads(result.splitlines()[0][6:])['choices'][0]
        self.assertEqual(choice['finish_reason'], 'stop')
        self.assertEqual(choice['delta'], {'content': 'Fixed and verified.'})

    def test_execute_alias_uses_offered_shell_tool(self):
        body = {'tools': [{'function': {'name': 'shell'}}]}
        choice = {'message': {'tool_calls': [{'function': {
            'name': 'execute', 'arguments': '{"command":"python3 check.py"}'}}]}}
        shim._repair_tool_arguments({'choices': [choice]}, body)
        self.assertEqual(choice['message']['tool_calls'][0]['function']['name'], 'shell')

    def test_path_alias_follows_each_clients_schema(self):
        for tool in ('read', 'edit', 'write'):
            for path_key in ('path', 'filePath'):
                alias = 'filePath' if path_key == 'path' else 'path'
                args = {alias: '/tmp/example.py', 'content': 'new content'}
                if tool == 'edit':
                    args.update(oldString='old', newString='new')
                body = {'tools': [{'function': {'name': tool, 'parameters': {
                    'properties': {path_key: {'type': 'string'}, 'content': {'type': 'string'}}}}}]}
                choice = {'message': {'tool_calls': [{'function': {
                    'name': tool, 'arguments': json.dumps(args)}}]}}
                shim._repair_tool_arguments({'choices': [choice]}, body)
                repaired = json.loads(choice['message']['tool_calls'][0]['function']['arguments'])
                self.assertEqual(repaired[path_key], '/tmp/example.py')
                self.assertNotIn(alias, repaired)

    def test_edit_aliases_follow_schema(self):
        body = {'tools': [{'function': {'name': 'edit', 'parameters': {
            'properties': {k: {} for k in ('path', 'oldString', 'newString', 'replaceAll')}}}}]}
        choice = {'message': {'tool_calls': [{'function': {'name': 'edit', 'arguments': json.dumps({
            'path': 'src/module.py', 'old_string': 'old', 'new_string': 'new', 'replace_all': False})}}]}}
        shim._repair_tool_arguments({'choices': [choice]}, body)
        args = json.loads(choice['message']['tool_calls'][0]['function']['arguments'])
        self.assertEqual(args, {'path': 'src/module.py', 'oldString': 'old', 'newString': 'new', 'replaceAll': False})

    def test_patch_text_needs_no_separate_path(self):
        args = {'patchText': '*** Begin Patch\n*** Update File: src/a.py\n@@\n-old\n+new\n*** End Patch'}
        body = {'tools': [{'function': {'name': 'apply_patch', 'parameters': {
            'properties': {'patchText': {'type': 'string'}}, 'required': ['patchText']}}}]}
        choice = {'message': {'tool_calls': [{'function': {
            'name': 'apply_patch', 'arguments': json.dumps(args)}}]}}
        shim._repair_tool_arguments({'choices': [choice]}, body)
        self.assertEqual(json.loads(choice['message']['tool_calls'][0]['function']['arguments']), args)

    def test_block_ends_turn_without_synthetic_execution(self):
        for command in ('pip install missing_module', 'cat > /tmp/demo.py <<EOF\nx\nEOF'):
            choice = {'message': {'tool_calls': [{'function': {
                'name': 'bash', 'arguments': json.dumps({'command': command})}}]},
                'finish_reason': 'tool_calls'}
            self.assertTrue(shim._repair_tool_arguments({'choices': [choice]}, BODY))
            self.assertEqual(choice['finish_reason'], 'stop')
            self.assertNotIn('tool_calls', choice['message'])
            self.assertIn('No command was executed', choice['message']['content'])

    def test_fragmented_stream_is_checked_after_assembly(self):
        lines = []
        for fn in ({'name': 'bash', 'arguments': '{"command":"pip '},
                   {'arguments': 'install missing_module"}'}):
            lines.append(b'data: ' + json.dumps({'choices': [{'delta': {
                'tool_calls': [{'index': 0, 'function': fn}]}, 'finish_reason': None}]}).encode())
        result = shim._guard_streamed_tool_calls(lines, BODY)
        event = json.loads(result.splitlines()[0][6:])
        self.assertEqual(event['choices'][0]['finish_reason'], 'stop')
        self.assertNotIn('tool_calls', event['choices'][0]['delta'])
        self.assertTrue(result.endswith(b'data: [DONE]\n\n'))

    def test_recovered_markup_is_also_guarded(self):
        event = {'choices': [{'delta': {'content':
            '<tool_call>{"name":"bash","arguments":{"command":"pip install missing_module"}}</tool_call>'}}]}
        result = shim._guard_streamed_tool_calls([b'data: ' + json.dumps(event).encode()], BODY)
        choice = json.loads(result.splitlines()[0][6:])['choices'][0]
        self.assertEqual(choice['finish_reason'], 'stop')
        self.assertNotIn('tool_calls', choice['delta'])

    def test_valid_script_and_requested_install_are_preserved(self):
        for command, messages in [('python3 -c "print(1)"', []),
                                  ('pip install requests', [{'role': 'user', 'content': 'Install requests'}])]:
            choice = {'message': {'tool_calls': [{'function': {
                'name': 'bash', 'arguments': json.dumps({'command': command}, separators=(',', ':'))}}]}}
            shim._repair_tool_arguments({'choices': [choice]}, {**BODY, 'messages': messages})
            self.assertEqual(json.loads(choice['message']['tool_calls'][0]['function']['arguments'])['command'], command)

    def test_existing_block_loop_stops_without_upstream_generation(self):
        for stream in (False, True):
            with patch.object(shim.fastflowlm, 'requests_paused', return_value=False), patch.object(shim.requests, 'post') as upstream:
                response = shim.app.test_client().post('/v1/chat/completions', json={
                    **BODY, 'model': 'minicpm-v-4.7:1b', 'stream': stream,
                    'messages': [{'role': 'user', 'content': 'Fix it'},
                                 {'role': 'tool', 'content': 'Hearth blocked an unsafe shell file-write/package-install command.'}]})
                self.assertEqual(response.status_code, 200)
                event = json.loads(response.data.splitlines()[0][6:]) if stream else response.json
                self.assertEqual(event['choices'][0]['finish_reason'], 'stop')
                upstream.assert_not_called()
