"""Optional local inference runtimes for first-time setup (no model downloads)."""
import platform
import ctypes.util
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.request import urlopen


def add_arguments(parser):
    parser.add_argument('--install-backends', nargs='+', choices=('lmstudio', 'lemonade'),
                        help='Install local runtimes: lmstudio (headless), lemonade (Ubuntu), or both')
    parser.add_argument('--skip-backend-install', action='store_true',
                        help='Skip the optional runtime installation prompt')


def select_backends(options, input_fn=input):
    if options.install_backends and options.skip_backend_install:
        raise ValueError('--install-backends and --skip-backend-install cannot be combined')
    if options.install_backends:
        return list(dict.fromkeys(options.install_backends))
    if options.skip_backend_install:
        return []
    print('Optional local runtimes: LM Studio headless (llmster); Lemonade (Ubuntu only).')
    while True:
        try:
            answer = input_fn('Install runtimes? [0 skip, 1 LM Studio, 2 Lemonade, 3 both; default 0]: ').strip() or '0'
        except EOFError:
            print('No interactive input; skipping optional runtime installation.')
            return []
        if answer in ('0', '1', '2', '3'):
            return {'0': [], '1': ['lmstudio'], '2': ['lemonade'],
                    '3': ['lmstudio', 'lemonade']}[answer]


def install_backends(backends):
    system = platform.system()
    # Validate the entire selection before making changes.
    if 'lemonade' in backends and (system != 'Linux' or platform.freedesktop_os_release().get('ID') != 'ubuntu'):
        raise RuntimeError('Automatic Lemonade installation requires Ubuntu. Use --skip-backend-install for manual setup.')
    if system not in ('Linux', 'Darwin', 'Windows'):
        raise RuntimeError('Automatic runtime installation supports Linux, macOS, and Windows.')
    for backend in backends:
        if backend == 'lmstudio':
            lms = shutil.which('lms')
            local = Path.home() / '.lmstudio/bin' / ('lms.exe' if system == 'Windows' else 'lms')
            if not lms and local.is_file():
                lms = str(local)
            if not lms:
                if system == "Linux" and (not ctypes.util.find_library("atomic") or not shutil.which("curl")):
                    if shutil.which("apt-get"):
                        subprocess.run(["sudo", "apt-get", "update"], check=True)
                        subprocess.run(["sudo", "apt-get", "install", "-y", "libatomic1", "curl", "ca-certificates"], check=True)
                    else:
                        raise RuntimeError("LM Studio requires libatomic and curl. Install them with your distribution package manager, then retry.")
                suffix = '.ps1' if system == 'Windows' else '.sh'
                url = 'https://lmstudio.ai/install' + suffix
                print(f'Installing LM Studio headless runtime from {url}')
                with tempfile.TemporaryDirectory(prefix='hearth-runtime-') as directory:
                    script = Path(directory) / ('install' + suffix)
                    if system != "Windows":
                        subprocess.run(["curl", "--fail", "--show-error", "--location",
                                        "--max-time", "120", "--output", str(script), url], check=True)
                    else:
                        with urlopen(url, timeout=60) as response:
                            script.write_bytes(response.read())
                    command = (['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script)]
                               if system == 'Windows' else ['bash', str(script)])
                    subprocess.run(command, check=True)
                lms = shutil.which('lms') or (str(local) if local.is_file() else None)
            if not lms:
                raise RuntimeError('LM Studio installer finished but lms was not found. Restart your terminal and retry.')
            subprocess.run([lms, 'daemon', 'up'], check=True)
            subprocess.run([lms, 'server', 'start', '--port', '1234'], check=True)
            print('LM Studio API started at http://localhost:1234 (no models downloaded).')
        elif backend == 'lemonade':
            if not (shutil.which('lemonade-server') or shutil.which('lemond')):
                # Preserve an existing snap installation rather than installing a second server.
                snap = subprocess.run(['snap', 'list', 'lemonade-server'], capture_output=True) if shutil.which('snap') else None
                if not snap or snap.returncode:
                    subprocess.run(['sudo', 'apt-get', 'update'], check=True)
                    subprocess.run(['sudo', 'apt-get', 'install', '-y', 'software-properties-common'], check=True)
                    subprocess.run(['sudo', 'add-apt-repository', '-y', 'ppa:lemonade-team/stable'], check=True)
                    subprocess.run(['sudo', 'apt-get', 'update'], check=True)
                    subprocess.run(['sudo', 'apt-get', 'install', '-y', 'lemonade-server'], check=True)
            if Path('/run/systemd/system').is_dir():
                print('Lemonade installed. Its package manages the service at http://localhost:13305.')
            else:
                print('Lemonade installed; systemd is unavailable. Start lemond manually to serve http://localhost:13305.')
            print('No models downloaded.')
