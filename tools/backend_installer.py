"""Optional local inference runtimes for first-time setup (no model downloads)."""
import platform
import ctypes.util
import os
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
            if not shutil.which('docker'):
                raise RuntimeError('Docker is required for the Lemonade backend. Install Docker first.')
            subprocess.run(['docker', 'pull', 'ghcr.io/lemonade-sdk/lemonade-server:latest'], check=True)
            npu_device = Path('/dev/accel/accel0')
            gpu_devices = [Path('/dev/kfd'), *sorted(Path('/dev/dri').glob('card[0-9]*')),
                           *sorted(Path('/dev/dri').glob('renderD*'))]
            gpu_devices = [device for device in gpu_devices if device.exists()]
            migrated_cache = Path.home() / 'lemonade-hf-cache'
            use_migrated_cache = migrated_cache.is_dir()
            docker_args = [
                'docker', 'run', '-d', '--name', 'lemonade-server',
                '--restart', 'unless-stopped', '-p', '13305:13305',
                '-v', 'lemonade-llama:/opt/lemonade/llama',
                '-v', 'lemonade-data:/opt/lemonade/.cache/lemonade',
                '-v', 'lemonade-config:/opt/lemonade/.config/lemonade',
            ]
            cache_mount = (f'{migrated_cache}:/opt/lemonade/.cache/huggingface'
                           if use_migrated_cache else
                           'lemonade-cache:/opt/lemonade/.cache/huggingface')
            docker_args += ['-v', cache_mount]
            if use_migrated_cache:
                print(f'Using migrated Lemonade model cache at {migrated_cache}.')
            if npu_device.exists():
                # FLM-recipe models use the AMD NPU. Pass through only the device
                # node and its owning group; Docker volumes keep model data when
                # the container is recreated to apply this device mapping.
                docker_args += ['--device', str(npu_device), '--group-add', str(os.stat(npu_device).st_gid)]
                print(f'Passing AMD NPU device {npu_device} into Lemonade Docker.')
            else:
                print(f'AMD NPU device {npu_device} is unavailable; Lemonade will use the model recipe’s available backend.')
            gpu_group_ids = set()
            for device in gpu_devices:
                docker_args += ['--device', str(device)]
                gpu_group_ids.add(os.stat(device).st_gid)
            for group_id in sorted(gpu_group_ids):
                docker_args += ['--group-add', str(group_id)]
            if gpu_devices:
                print('Passing host GPU devices and owning groups into Lemonade Docker for Vulkan/ROCm models.')

            inspect = subprocess.run(
                ['docker', 'inspect', '-f', '{{json .HostConfig.Devices}} {{json .HostConfig.GroupAdd}} {{json .Mounts}}', 'lemonade-server'],
                capture_output=True, text=True,
            )
            already_configured = inspect.returncode == 0 and (
                not npu_device.exists() or str(npu_device) in inspect.stdout
            ) and all(str(device) in inspect.stdout for device in gpu_devices) and all(
                str(os.stat(device).st_gid) in inspect.stdout for device in gpu_devices
            ) and (not use_migrated_cache or str(migrated_cache) in inspect.stdout)
            if not already_configured:
                if inspect.returncode == 0:
                    subprocess.run(['docker', 'rm', '-f', 'lemonade-server'], check=True)
                subprocess.run(docker_args + ['ghcr.io/lemonade-sdk/lemonade-server:latest'], check=True)
            print('Lemonade Docker container started at http://localhost:13305.')
            print('No models downloaded.')
