import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

sys_path = os.path.join(os.path.dirname(__file__), "..", "deploy")


class BackendBrokerTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "backend_broker", os.path.join(os.path.dirname(__file__), "..", "deploy", "backend_broker.py")
        )
        self.broker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.broker)
        self.broker._ALLOWED_UNITS = frozenset({"vllm.service", "kobold.service", "flm.service"})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.broker._Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _client_env(self, token):
        return {
            "HEARTH_BACKEND_BROKER_URL": self.url,
            "HEARTH_BACKEND_BROKER_TOKEN": token,
        }

    def test_rejects_unknown_service_and_verb(self):
        with patch.dict(os.environ, {"HEARTH_BACKEND_BROKER_TOKEN": "token"}):
            for body in (
                {"args": ["restart"], "service": "unheard.service"},
                {"args": ["kill"], "service": "vllm.service"},
            ):
                status, payload = self.patch_broker(body=body)
                self.assertEqual(status, 400, payload)

    def patch_broker(self, body, token="token"):
        import json as jsonlib
        import urllib.request

        request = urllib.request.Request(
            f"{self.url}/systemd",
            data=jsonlib.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as resp:
                return resp.status, jsonlib.loads(resp.read().decode())
        except urllib.error.HTTPError as hit:
            return hit.code, jsonlib.loads(hit.read().decode())

    def test_rejects_wrong_token(self):
        import inspect

        self.assertIn("invalid broker token", inspect.getsource(self.broker))

    def test_rejects_wrong_verb_and_unknown_service(self):
        with patch.dict(os.environ, {"HEARTH_BACKEND_BROKER_TOKEN": "token"}):
            with self.assertRaises(self.broker.BrokerError):
                self.broker.run_systemctl(["kill"], "vllm.service")
            with self.assertRaises(self.broker.BrokerError):
                self.broker.run_systemctl(["restart"], "not-a-hearth.service")

    def test_rejects_unsupported_flm_verb(self):
        with patch.dict(os.environ, {"HEARTH_BACKEND_BROKER_TOKEN": "token"}):
            with self.assertRaises(self.broker.BrokerError):
                self.broker.run_flm(["serve", "qwen3:0.6b"])
            with self.assertRaises(self.broker.BrokerError):
                self.broker.run_flm(["list", "--bad"])

    def test_flm_version_is_allowed_when_binary_is_configured(self):
        # Dot `flm --version` is read-only; the broker refuses to start serving.
        with patch.dict(os.environ, {
            "HEARTH_BACKEND_BROKER_TOKEN": "token",
            "HEARTH_BACKEND_BROKER_FLM_BIN": "/bin/true",
        }):
            result = self.broker.run_flm(["--version"])
            self.assertEqual(result["returncode"], 0)
            with self.assertRaises(self.broker.BrokerError):
                self.broker.run_flm(["serve", "x"])


class BackendBrokerClientTests(unittest.TestCase):
    def setUp(self):
        import importlib
        self.client = importlib.import_module("hearth.backends.backend_broker_client")

    def test_no_broker_configured_returns_none(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(
                self.client.systemctl_via_broker(["restart"], "vllm.service"),
            )

    def test_local_systemctl_is_preferred_when_available(self):
        with patch.dict(os.environ, self._client_env("token")), \
             patch("shutil.which", return_value="/bin/systemctl"), \
             patch("requests.post", side_effect=AssertionError("must not hit broker")):
            self.assertIsNone(self.client.systemctl_via_broker(["stop"], "vllm.service"))

    def _client_env(self, token):
        return {
            "HEARTH_BACKEND_BROKER_URL": "http://127.0.0.1:8765",
            "HEARTH_BACKEND_BROKER_TOKEN": token,
        }

    def test_broker_error_surfaces_message(self):
        import requests as requests_module

        class StubResponse:
            status_code = 400
            def json(self):
                return {"error": "unsupported systemctl verb 'kill'"}

        with patch.dict(os.environ, self._client_env("token")), \
             patch("shutil.which", return_value=None), \
             patch.object(requests_module, "post", return_value=StubResponse()):
            with self.assertRaises(self.client.BackendBrokerError) as caught:
                self.client.systemctl_via_broker(["kill"], "vllm.service")
        self.assertIn("unsupported systemctl verb", str(caught.exception))

    def test_broker_success_returns_stdout(self):
        import requests as requests_module

        class StubResponse:
            status_code = 200
            def json(self):
                return {"returncode": 0, "stdout": "active\n", "stderr": ""}

        with patch.dict(os.environ, self._client_env("token")), \
             patch("shutil.which", return_value=None), \
             patch.object(requests_module, "post", return_value=StubResponse()):
            self.assertEqual(
                self.client.systemctl_via_broker(["is-active"], "vllm.service"),
                "active\n",
            )


if __name__ == '__main__':
    unittest.main()
