import unittest
from unittest.mock import Mock, patch
import requests
from agent_opencode import OpenCode


class OpenCodeAdapterTests(unittest.TestCase):
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
