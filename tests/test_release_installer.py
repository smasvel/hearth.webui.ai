import unittest
from unittest.mock import patch

from tools.release_installer import BACKENDS, choose_backend


class ReleaseInstallerBackendTests(unittest.TestCase):
    def test_platform_backend_choices(self):
        self.assertEqual(
            {key for key, *unused in BACKENDS.items() if "Windows" in BACKENDS[key][2]},
            {"lmstudio", "kobold", "vllm", "sglang"},
        )
        self.assertEqual(
            {key for key, *unused in BACKENDS.items() if "Darwin" in BACKENDS[key][2]},
            {"lmstudio", "kobold", "vllm", "sglang"},
        )

    def test_linux_can_select_npu_backend(self):
        answers = iter(["6", "http://127.0.0.1:52625"])
        with patch("builtins.print"):
            selected = choose_backend("Linux", lambda _prompt: next(answers))
        self.assertEqual(selected, ("fastflowlm", "http://127.0.0.1:52625"))

    def test_macos_does_not_offer_linux_only_runtime(self):
        answers = iter(["1", ""])
        with patch("builtins.print") as output:
            selected = choose_backend("Darwin", lambda _prompt: next(answers))
        self.assertEqual(selected, ("lmstudio", "http://localhost:1234"))
        rendered = " ".join(str(call) for call in output.call_args_list)
        self.assertNotIn("FastFlowLM", rendered)
        self.assertNotIn("Lemonade", rendered)


class ReleaseInstallerAutomationTests(unittest.TestCase):
    def test_invalid_port_fails_before_docker_or_prompt(self):
        from tools import release_installer
        with patch.object(release_installer, '_run') as run:
            with self.assertRaises(SystemExit):
                release_installer.install(['--postgres-port', '0'])
        run.assert_not_called()

    def test_resolved_backend_equals_arguments_are_not_duplicated(self):
        import os
        from tools import release_installer
        args = ['--backend=lmstudio', '--backend-url=http://remote:1234',
                '--skip-backend-install', '--skip-browser', '--no-api-key']
        with patch.dict(os.environ, {'HEARTH_RELEASE_INSTALL_IN_VENV': '0'}), \
             patch.object(release_installer.platform, 'system', return_value='Linux'), \
             patch.object(release_installer.shutil, 'which', return_value='/usr/bin/docker'), \
             patch.object(release_installer, '_run'), \
             patch.object(release_installer.subprocess, 'run') as run:
            release_installer.install(args)
        forwarded = run.call_args.args[0]
        self.assertIn('--backend=lmstudio', forwarded)
        self.assertNotIn('--backend', forwarded)
        self.assertNotIn('--backend-url', forwarded)
        self.assertEqual(forwarded.count('--skip-backend-install'), 1)
        self.assertIn('--no-api-key', forwarded)


    def test_linux_browser_install_includes_system_dependencies(self):
        import os
        from tools import release_installer
        with patch.dict(os.environ, {'HEARTH_RELEASE_INSTALL_IN_VENV': '0'}), \
             patch.object(release_installer.platform, 'system', return_value='Linux'), \
             patch.object(release_installer.shutil, 'which', return_value='/usr/bin/docker'), \
             patch.object(release_installer, '_run') as run, \
             patch.object(release_installer.subprocess, 'run'):
            release_installer.install(['--backend', 'lmstudio', '--skip-backend-install', '--no-api-key'])
        browser = [call.args[0] for call in run.call_args_list if 'playwright' in call.args[0]]
        self.assertEqual(len(browser), 1)
        self.assertEqual(browser[0][-4:], ['playwright', 'install', '--with-deps', 'chromium'])


    def test_legacy_data_blocks_runtime_and_agent_installation(self):
        import os
        from tools import release_installer, first_time_setup, backend_installer
        with patch.dict(os.environ, {'HEARTH_RELEASE_INSTALL_IN_VENV': '1'}), \
             patch.object(release_installer.platform, 'system', return_value='Linux'), \
             patch.object(release_installer.shutil, 'which', return_value='/usr/bin/docker'), \
             patch.object(release_installer, '_run'), \
             patch.object(first_time_setup, 'validate_existing_installation', side_effect=RuntimeError('Existing local database')), \
             patch.object(first_time_setup, 'install_agent_runtime') as agent, \
             patch.object(backend_installer, 'install_backends') as runtime:
            with self.assertRaisesRegex(RuntimeError, 'Existing local database'):
                release_installer.install(['--backend', 'lmstudio', '--install-backends', 'lmstudio', '--no-api-key'])
        agent.assert_not_called()
        runtime.assert_not_called()


if __name__ == "__main__":
    unittest.main()
