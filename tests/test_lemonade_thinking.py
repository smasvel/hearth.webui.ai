import copy
import unittest
from unittest.mock import patch

from hearth.backends import lemonade_client
from hearth.backends import lemonade_guard_shim as shim
from hearth.backends import lemonade_thinking


MODEL = 'Ornith-1.5-35B-A3B-GGUF'


class LemonadeThinkingTests(unittest.TestCase):
    def setUp(self):
        setting = patch.object(lemonade_thinking.settings_store, 'get',
                               return_value='{"' + MODEL + '":false}')
        setting.start()
        self.addCleanup(setting.stop)

    def test_disabled_model_overrides_stale_client_flags_without_mutating_input(self):
        body = {'model': MODEL, 'enable_thinking': True,
                'chat_template_kwargs': {'enable_thinking': True, 'preserve_thinking': True}}
        original = copy.deepcopy(body)
        result = lemonade_thinking.apply(body)
        self.assertFalse(result['enable_thinking'])
        self.assertEqual(result['chat_template_kwargs'],
                         {'enable_thinking': False, 'preserve_thinking': True})
        self.assertEqual(body, original)
        self.assertIs(lemonade_thinking.apply({'model': 'other'}).get('enable_thinking'), None)

    def test_enabled_or_invalid_saved_preferences_preserve_request_choice(self):
        body = {'model': MODEL, 'enable_thinking': False}
        for saved in ('{"' + MODEL + '":true}', '[]', 'invalid', '{"' + MODEL + '":0}'):
            with self.subTest(saved=saved), patch.object(lemonade_thinking.settings_store, 'get', return_value=saved):
                self.assertIs(lemonade_thinking.apply(body), body)

    def test_gateway_enforces_preference_before_forwarding_plain_and_tool_requests(self):
        for tools in ([], [{'type': 'function', 'function': {'name': 'inspect_project'}}]):
            with self.subTest(tools=tools), \
                    patch.object(shim.model_test_profiles, 'apply', side_effect=lambda body, *_: body), \
                    patch.object(shim, '_is_npu_model', return_value=False), \
                    patch.object(shim, '_unload_before_implicit_load'), \
                    patch.object(shim.live_report, 'start'), \
                    patch.object(shim.live_report, 'finish'), \
                    patch.object(shim, '_record_chat_metric'), \
                    patch.object(shim, '_post_chat_with_utf8_recovery', side_effect=shim.requests.Timeout) as post:
                response = shim.app.test_client().post('/v1/chat/completions', json={
                    'model': MODEL, 'messages': [{'role': 'user', 'content': 'Inspect the project.'}],
                    'tools': tools, 'enable_thinking': True,
                    'chat_template_kwargs': {'enable_thinking': True}})
                self.assertEqual(response.status_code, 504)
                forwarded = post.call_args.args[0]
                self.assertFalse(forwarded['enable_thinking'])
                self.assertFalse(forwarded['chat_template_kwargs']['enable_thinking'])

    def test_direct_chat_enforces_saved_preference(self):
        with patch.object(lemonade_client.model_test_profiles, 'apply', side_effect=lambda body, *_: body), \
                patch.object(lemonade_client, '_auth_headers', return_value={}), \
                patch.object(lemonade_client, 'base_url', return_value='http://localhost:13305'), \
                patch.object(lemonade_client.requests, 'post') as post:
            post.return_value.__enter__.return_value.iter_lines.return_value = []
            list(lemonade_client.chat_stream([], MODEL, enable_thinking=True))
            forwarded = post.call_args.kwargs['json']
            self.assertFalse(forwarded['enable_thinking'])
            self.assertFalse(forwarded['chat_template_kwargs']['enable_thinking'])
