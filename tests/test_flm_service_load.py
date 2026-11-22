"""Set HEARTH_LIVE_FLM_MODEL to an installed flm/<tag> to exercise a real NPU handoff."""
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from hearth.backends import flm_serve

ROOT = Path(__file__).resolve().parents[1]


class FastFlowLMServiceTests(unittest.TestCase):
    def test_installed_service_entrypoint_starts_selected_model(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            active = home / '.config/fastflowlm/active'
            active.parent.mkdir(parents=True)
            active.write_text('minicpm-v-4.7:1b\n')
            with patch.object(Path, 'home', return_value=home), patch('os.execv') as launch:
                runpy.run_path(str(ROOT / 'flm_serve.py'), run_name='__main__')
            args = flm_serve.command('minicpm-v-4.7:1b')
            launch.assert_called_once_with(args[0], args)

    def test_busy_npu_returns_conflict_before_eviction(self):
        import app
        from tests._side_test_helpers import as_admin, csrf, patch_admin

        with (
            patch_admin(),
            patch.object(app._agent_tasks, 'busy', return_value=False),
            patch.object(app._model_test_registry, 'list', return_value=[]),
            patch.object(app, 'model_metadata_store'),
            patch.object(app, '_model_load_status', {}),
            patch.object(app.fastflowlm, 'check_loadable'),
            patch.object(app.npu_request_lock, 'acquire', side_effect=TimeoutError('NPU is busy')) as acquire,
            patch.object(app, '_free_npu_except') as handoff,
            patch.object(app, '_unload_reasoning_models_before_load') as evict,
            patch.object(app.fastflowlm, 'load_model') as load,
        ):
            response = as_admin().post('/api/models/load', json={'model': 'flm/test'}, headers=csrf())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['error'], 'NPU is busy')
        acquire.assert_called_once_with(timeout=15)
        handoff.assert_not_called()
        evict.assert_not_called()
        load.assert_not_called()

    @unittest.skipUnless(os.environ.get('HEARTH_LIVE_FLM_MODEL'), 'requires an installed FLM model')
    def test_live_model_load_then_generate(self):
        # Bound the entire handoff, including a load route blocked on the NPU lock.
        subprocess.run([sys.executable, '-m', 'tests.test_flm_service_load'],
                       cwd=ROOT, timeout=180, check=True)


def verify_live_load():
    import psutil
    import requests
    from hearth.backends import flm_context
    from hearth.backends import fastflowlm_client as flm
    from tests._side_test_helpers import as_admin, csrf, patch_admin

    model = os.environ['HEARTH_LIVE_FLM_MODEL']
    if not flm.owns_model(model):
        raise ValueError('Expected a standalone flm/<tag> model')
    with patch_admin():
        response = as_admin().post('/api/models/load', json={'model': model}, headers=csrf())
    if response.status_code != 200 or not response.get_json().get('ok'):
        raise AssertionError(f'FLM load failed ({response.status_code}): {response.get_json()}')

    pid = int(subprocess.check_output(['systemctl', 'show', 'flm.service', '-p', 'MainPID', '--value']))
    args = psutil.Process(pid).cmdline()
    actual = int(args[args.index('--ctx-len') + 1])
    expected = min(65536, flm_context.supported_context(model))
    if actual != expected:
        raise AssertionError(f'FLM started with {actual} context tokens, expected {expected}')

    result = requests.post('http://127.0.0.1:52626/v1/chat/completions', timeout=90, json={
        'model': flm.resolve_model(model),
        'messages': [{'role': 'user', 'content': 'Reply with OK.'}],
        'max_tokens': 64,
    })
    result.raise_for_status()
    choice = result.json()['choices'][0]
    if not (choice['message'].get('content') or '').strip() or choice.get('finish_reason') != 'stop':
        raise AssertionError('FLM did not return a visible, completed reply')


if __name__ == '__main__':
    verify_live_load()
