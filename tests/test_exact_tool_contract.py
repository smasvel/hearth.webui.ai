import copy
import json
import unittest
from unittest.mock import Mock

import exact_tool_contract as contract
import flm_tool_shim as flm
from tools.opencode_scenarios import TOOLS
from tools.test_flm_model import decode

ARGS = {'filePath': '/project/x.py', 'oldString': 'return price + tax_rate', 'newString': 'return price + price * tax_rate'}


def body():
    return {'tools': TOOLS, 'messages': [{'role': 'user', 'content': 'Next call edit with exactly these arguments: ' + json.dumps(ARGS) + '.'}]}


def payload(args=None):
    return {'choices': [{'index': 0, 'message': {'tool_calls': [{'id': 'real-call', 'function': {
        'name': 'edit', 'arguments': json.dumps(args or {**ARGS, 'replaceAll': True})}}]}, 'finish_reason': 'tool_calls'}]}


class ExactToolContractTests(unittest.TestCase):
    def test_drops_only_unrequested_optional_fields(self):
        p = payload()
        self.assertTrue(contract.repair_completion(p, body()))
        self.assertEqual(json.loads(p['choices'][0]['message']['tool_calls'][0]['function']['arguments']), ARGS)
        self.assertFalse(contract.repair_completion(p, body()))
        explicit = body(); explicit['messages'][0]['content'] = 'Next call edit with exactly: ' + json.dumps({**ARGS, 'replaceAll': True})
        p = payload()
        self.assertFalse(contract.repair_completion(p, explicit))
        self.assertTrue(json.loads(p['choices'][0]['message']['tool_calls'][0]['function']['arguments'])['replaceAll'])

    def test_does_not_change_paths_code_arrays_types_or_missing_arguments(self):
        for args in [{**ARGS, 'replaceAll': True, 'filePath': '/other'},
                     {**ARGS, 'replaceAll': True, 'newString': 'different code'},
                     {**ARGS, 'unknown': True}, {'filePath': '/project/x.py'}]:
            p = payload(args); before = copy.deepcopy(p)
            self.assertFalse(contract.repair_completion(p, body()))
            self.assertEqual(p, before)

    def test_requires_latest_explicit_user_contract_and_single_completed_call(self):
        for kind in ('tool-message', 'new-user', 'ambiguous', 'multiple', 'truncated', 'required-extra'):
            b, p = body(), payload()
            if kind == 'tool-message': b['messages'][0]['role'] = 'tool'
            if kind == 'new-user': b['messages'].append({'role': 'user', 'content': 'Do something else'})
            if kind == 'ambiguous': b['messages'][0]['content'] += ' Also update another file.'
            if kind == 'multiple': p['choices'][0]['message']['tool_calls'] *= 2
            if kind == 'truncated': p['choices'][0]['finish_reason'] = 'length'
            if kind == 'required-extra':
                b['tools'] = copy.deepcopy(TOOLS)
                next(t['function']['parameters']['required'] for t in b['tools'] if t['function']['name'] == 'edit').append('replaceAll')
            self.assertFalse(contract.repair_completion(p, b), kind)

    def test_flm_and_fragmented_stream_preserve_call_identity(self):
        p = payload()
        self.assertTrue(flm._repair_tool_arguments(p, body()))
        self.assertEqual(json.loads(p['choices'][0]['message']['tool_calls'][0]['function']['arguments']), ARGS)
        raw = json.dumps({**ARGS, 'replaceAll': True})
        events = [{'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0, 'id': 'real-call',
                   'function': {'name': 'edit', 'arguments': raw[:30]}}]}}]},
                  {'choices': [{'index': 0, 'delta': {'tool_calls': [{'index': 0,
                   'function': {'arguments': raw[30:]}}]}, 'finish_reason': 'tool_calls'}], 'usage': {'completion_tokens': 44}}]
        lines = [b'data: ' + json.dumps(e).encode() + b'\n' for e in events] + [b'data: [DONE]\n']
        calls, _, finishes, usage = decode(Mock(content=b''.join(contract.repair_sse(lines, body()))), True)
        self.assertEqual(calls[0]['arguments'], ARGS)
        self.assertEqual(calls[0]['id'], 'real-call')
        self.assertEqual(finishes, ['tool_calls'])
        self.assertEqual(usage['completion_tokens'], 44)
