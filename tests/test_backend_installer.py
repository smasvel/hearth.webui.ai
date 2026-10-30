import argparse
import subprocess
import unittest
from unittest.mock import patch
from tools import backend_installer as installer


class BackendInstallerTests(unittest.TestCase):
    def options(self, *args):
        parser = argparse.ArgumentParser()
        installer.add_arguments(parser)
        return parser.parse_args(args)

    def test_skip_and_explicit_selection_never_prompt(self):
        def unexpected(_):
            self.fail('Unexpected prompt')
        self.assertEqual(installer.select_backends(self.options('--skip-backend-install'), unexpected), [])
        self.assertEqual(installer.select_backends(self.options('--install-backends', 'lmstudio', 'lemonade', 'lmstudio'), unexpected), ['lmstudio', 'lemonade'])
        with self.assertRaises(ValueError):
            installer.select_backends(self.options('--install-backends', 'lmstudio', '--skip-backend-install'), unexpected)

    def test_prompt_both_and_default_skip(self):
        self.assertEqual(installer.select_backends(self.options(), lambda _: '3'), ['lmstudio', 'lemonade'])
        self.assertEqual(installer.select_backends(self.options(), lambda _: ''), [])

    @patch.object(installer.platform, 'system', return_value='Darwin')
    @patch.object(installer.subprocess, 'run')
    def test_unsupported_combination_installs_nothing(self, run, system):
        with self.assertRaisesRegex(RuntimeError, 'Ubuntu'):
            installer.install_backends(['lmstudio', 'lemonade'])
        run.assert_not_called()

    @patch.object(installer.platform, 'system', return_value='Linux')
    @patch.object(installer.shutil, 'which', return_value='/usr/bin/lms')
    @patch.object(installer.subprocess, 'run')
    def test_existing_lms_started_without_download(self, run, which, system):
        with patch.object(installer, 'urlopen') as download:
            installer.install_backends(['lmstudio'])
        download.assert_not_called()
        self.assertEqual([c.args[0] for c in run.call_args_list], [
            ['/usr/bin/lms', 'daemon', 'up'], ['/usr/bin/lms', 'server', 'start', '--port', '1234']])

    @patch.object(installer.platform, 'system', return_value='Linux')
    @patch.object(installer.platform, 'freedesktop_os_release', return_value={'ID': 'ubuntu'})
    @patch.object(installer.shutil, 'which', return_value=None)
    @patch.object(installer.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, ['sudo', 'apt-get', 'update']))
    def test_package_failure_stops_installation(self, run, which, release, system):
        with self.assertRaises(subprocess.CalledProcessError):
            installer.install_backends(['lemonade'])
        self.assertEqual(run.call_count, 1)

    @patch.object(installer.platform, 'system', return_value='Linux')
    @patch.object(installer.platform, 'freedesktop_os_release', return_value={'ID': 'ubuntu'})
    @patch.object(installer.shutil, 'which', return_value=None)
    @patch.object(installer.subprocess, 'run')
    def test_ubuntu_installs_from_official_ppa(self, run, which, release, system):
        installer.install_backends(['lemonade'])
        commands = [c.args[0] for c in run.call_args_list]
        self.assertIn(['sudo', 'add-apt-repository', '-y', 'ppa:lemonade-team/stable'], commands)
        self.assertEqual(commands[-1], ['sudo', 'apt-get', 'install', '-y', 'lemonade-server'])

    @patch.object(installer.platform, 'system', return_value='Windows')
    @patch.object(installer.Path, 'is_file', side_effect=[False, True])
    @patch.object(installer.shutil, 'which', return_value=None)
    @patch.object(installer.subprocess, 'run')
    def test_windows_downloads_official_script_and_starts_api(self, run, which, exists, system):
        with patch.object(installer, 'urlopen') as download:
            download.return_value.__enter__.return_value.read.return_value = b'# installer'
            installer.install_backends(['lmstudio'])
        download.assert_called_once_with('https://lmstudio.ai/install.ps1', timeout=60)
        self.assertEqual(run.call_args_list[0].args[0][:5], ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File'])
        self.assertEqual(run.call_args_list[-1].args[0][1:], ['server', 'start', '--port', '1234'])

    def test_closed_input_skips_optional_install(self):
        def closed(_):
            raise EOFError
        self.assertEqual(installer.select_backends(self.options(), closed), [])

    @patch.object(installer.platform, 'system', return_value='Linux')
    @patch.object(installer.platform, 'freedesktop_os_release', return_value={'ID': 'ubuntu'})
    @patch.object(installer.shutil, 'which', side_effect=lambda cmd: '/usr/bin/lemond' if cmd == 'lemond' else None)
    @patch.object(installer.subprocess, 'run')
    def test_current_lemonade_package_is_reused(self, run, which, release, system):
        installer.install_backends(['lemonade'])
        run.assert_not_called()

    @patch.object(installer.platform, 'system', return_value='Linux')
    @patch.object(installer.ctypes.util, 'find_library', return_value=None)
    @patch.object(installer.Path, 'is_file', side_effect=[False, True])
    @patch.object(installer.shutil, 'which', side_effect=lambda cmd: '/usr/bin/' + cmd if cmd in ('apt-get', 'curl') else None)
    @patch.object(installer.subprocess, 'run')
    def test_clean_linux_installs_libatomic_and_downloads_with_curl(self, run, which, exists, library, system):
        installer.install_backends(['lmstudio'])
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(['sudo', 'apt-get', 'install', '-y', 'libatomic1', 'curl', 'ca-certificates'], commands)
        self.assertEqual(commands[2][0], 'curl')
        self.assertEqual(commands[2][-1], 'https://lmstudio.ai/install.sh')
