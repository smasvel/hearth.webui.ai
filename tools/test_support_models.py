"""Live endpoint checks for downloaded embedding, speech and video models.

Run after the chat matrix so support checks never compete for model ownership.
Only local admin API actions are used for chat/embedding/voice handoffs.
"""
import argparse
import json
import math
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requests
import urllib3
import app
import comfyui_client
import lemonade_client

urllib3.disable_warnings()
ROOT = Path(__file__).resolve().parents[1]


def session():
    admin = next(u for u in app.users_store.list_users(per_page=100)['users'] if u['role'] == 'admin')
    csrf = secrets.token_urlsafe(32)
    cookie = app.app.session_interface.get_signing_serializer(app.app).dumps({
        'logged_in': True, 'user_id': admin['id'], 'username': admin['username'],
        'role': 'admin', 'last_activity': time.time(), 'login_csrf_token': csrf})
    client = requests.Session()
    client.verify = False
    client.cookies.set(app.app.config['SESSION_COOKIE_NAME'], cookie, domain='127.0.0.1', path='/')
    client.headers['X-CSRF-Token'] = csrf
    return client


def api(client, path, body=None):
    url = 'https://127.0.0.1:5443' + path
    response = client.get(url, timeout=30) if body is None else client.post(url, json=body, timeout=360)
    response.raise_for_status()
    return response.json()


def probe_media(data, suffix, still=False):
    with tempfile.TemporaryDirectory(prefix='hearth-support-probe-') as directory:
        path = Path(directory) / ('sample' + suffix)
        path.write_bytes(data)
        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                                '-of', 'json', str(path)], capture_output=True, text=True, check=True)
        info = json.loads(probe.stdout)
        assert info['streams'], info
        if not still:
            assert float(info['format']['duration']) > 0, info
        return info


def run(after_job, output):
    client = session()
    if after_job:
        if len(after_job) != 32 or any(c not in '0123456789abcdef' for c in after_job):
            raise ValueError('Invalid chat matrix job ID')
        while True:
            job = api(client, '/api/model-tests/' + after_job)
            if job['status'] == 'completed':
                break
            if job['status'] not in ('loading', 'running', 'repairing', 'cancelling'):
                raise RuntimeError('Chat matrix did not complete: ' + job['status'])
            time.sleep(10)
    results = []
    inventory = api(client, '/api/models')['models']
    targets = [m for m in inventory if m.get('type') in ('embeddings', 'transcription', 'tts', 'video')]
    checkpoint = Path(comfyui_client.COMFYUI_MODELS_DIR) / 'checkpoints' / comfyui_client.CHECKPOINT
    if checkpoint.is_file() and checkpoint.stat().st_size:
        targets.append({'backend': 'comfyui', 'id': comfyui_client.CHECKPOINT, 'type': 'image'})
    # TTS before STT permits a real speech round trip rather than silence-only ASR.
    targets.sort(key=lambda m: {'embeddings': 0, 'tts': 1, 'transcription': 2, 'image': 3, 'video': 4}[m['type']])
    spoken = None
    for model in targets:
        result = {'backend': model['backend'], 'model': model['id'], 'type': model['type']}
        started = time.monotonic()
        try:
            client = session()
            # The API refuses changes during new agent assignments or model tests.
            for loaded in api(client, '/api/models')['models']:
                if loaded.get('state') == 'loaded' and loaded.get('backend') != 'sglang':
                    api(client, '/api/models/unload', {'model': loaded['id']})
            if model['type'] not in ('image', 'video'):
                api(client, '/api/models/load', {'model': model['id']})
                assert any(m['id'] == model['id'] and m['backend'] == model['backend'] and m['state'] == 'loaded'
                           for m in api(client, '/api/models')['models']), 'Target not loaded'
            if model['type'] == 'embeddings':
                backend = app.lms if model['backend'] == 'lmstudio' else lemonade_client
                response = requests.post(backend.base_url() + '/v1/embeddings',
                    headers=backend._auth_headers(), json={'model': model['id'],
                    'input': ['Hearth model validation', 'Hearth model validation']}, timeout=120)
                response.raise_for_status()
                vectors = [x['embedding'] for x in sorted(response.json()['data'], key=lambda x: x['index'])]
                assert len(vectors) == 2 and len(vectors[0]) > 0 and len(vectors[0]) == len(vectors[1])
                assert all(math.isfinite(n) for v in vectors for n in v)
                norm = math.sqrt(sum(n*n for n in vectors[0]))
                assert norm > 0 and max(abs(a-b) for a,b in zip(*vectors)) < 0.001
                result['dimensions'] = len(vectors[0])
            elif model['type'] == 'tts':
                spoken = lemonade_client.synthesize_speech('Hearth model validation is complete.')
                info = probe_media(spoken, '.mp3')
                assert info['streams'][0]['codec_type'] == 'audio'
                result['audio_bytes'] = len(spoken)
            elif model['type'] == 'transcription':
                assert spoken, 'Speech round-trip blocked: TTS did not produce valid audio'
                text = lemonade_client.transcribe_audio(spoken, 'sample.mp3')
                assert 'validation' in text.lower() and 'complete' in text.lower(), text
                result['transcript'] = text
            elif model['type'] == 'image':
                generated = comfyui_client.generate_image('A red ball on a plain white background.',
                    width=256, height=256, steps=2, seed=5829)
                info = probe_media(generated['image_bytes'], '.png', still=True)
                picture = info['streams'][0]
                assert (picture['width'], picture['height']) == (256, 256) and picture['codec_name'] == 'png'
                result['image_bytes'] = len(generated['image_bytes'])
            else:
                generated = comfyui_client.generate_video('A red ball on a plain white background.',
                    frames=5, steps=12, seed=5829, timeout=3600)
                info = probe_media(generated['video_bytes'], '.mp4')
                assert any(s['codec_type'] == 'video' for s in info['streams'])
                result['video_bytes'] = len(generated['video_bytes'])
            result['passed'] = True
        except Exception as exc:
            result.update(passed=False, error=str(exc)[:1500])
        result['seconds'] = round(time.monotonic() - started, 2)
        results.append(result)
        Path(output).write_text(json.dumps({'results': results}, indent=2))
        print(json.dumps(result), flush=True)
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--after-job')
    parser.add_argument('--output', default='/tmp/hearth-support-model-tests.json')
    args = parser.parse_args()
    results = run(args.after_job, args.output)
    raise SystemExit(0 if all(r['passed'] for r in results) else 1)
