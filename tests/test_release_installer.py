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


if __name__ == "__main__":
    unittest.main()
