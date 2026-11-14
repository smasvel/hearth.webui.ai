"""Wire Caddy's existing authenticated LM Studio routes through its compatibility proxy.

Run as the user service's ExecStartPost so routing is restored at login/boot without
editing root-owned files or changing credentials. Other backends are untouched.
"""
import time
import requests


def upstream_paths(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            child = path + '/' + key
            if key == 'dial' and item in ('localhost:1234', '127.0.0.1:1234'):
                yield child
            else:
                yield from upstream_paths(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from upstream_paths(item, path + '/' + str(index))


def wire():
    base = 'http://127.0.0.1:2019/config/apps/http/servers'
    response = requests.get(base, timeout=3)
    response.raise_for_status()
    paths = list(upstream_paths(response.json()))
    for path in paths:
        result = requests.patch(base + path, json='127.0.0.1:13307', timeout=5)
        result.raise_for_status()
    print(f'LM Studio compatibility routing ready ({len(paths)} upstreams updated).', flush=True)


if __name__ == '__main__':
    for attempt in range(30):
        try:
            wire()
            break
        except requests.RequestException:
            if attempt == 29:
                raise
            time.sleep(1)
