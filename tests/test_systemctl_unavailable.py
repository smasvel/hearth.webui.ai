import unittest
import unittest.mock

import kobold_client
import vllm_client as vllm


def _raise_missing(*args, **kwargs):
    raise FileNotFoundError("no systemctl here")


class SystemctlUnavailableTests(unittest.TestCase):
    """Docker Hearth has no systemd; the host systemd units must surface as a normal
    backend error instead of a raw 500 page the browser cannot parse."""

    def test_vllm_systemctl_missing_is_wrapped(self):
        with unittest.mock.patch.object(
            vllm.subprocess, "run", side_effect=_raise_missing
        ):
            with self.assertRaises(vllm.VLLMError) as caught:
                vllm._systemctl("stop")
        self.assertIn("Docker app container has no systemd", str(caught.exception))

    def test_kobold_systemctl_missing_is_wrapped(self):
        with unittest.mock.patch.object(
            kobold_client.subprocess, "run", side_effect=_raise_missing
        ):
            with self.assertRaises(kobold_client.KoboldError) as caught:
                kobold_client._systemctl("stop")
        self.assertIn("Docker app container has no systemd", str(caught.exception))


if __name__ == '__main__':
    unittest.main()
