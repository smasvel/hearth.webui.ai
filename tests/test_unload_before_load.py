import unittest
import sys
import types
from unittest.mock import Mock, patch

# The production shims use Flask only to register routes. Unit tests exercise their
# model-handoff helpers and keep the repository's dependency-light test environment.
try:
    import flask  # noqa: F401
except ModuleNotFoundError:
    flask_stub = types.ModuleType("flask")

    class _Flask:
        def __init__(self, *_args, **_kwargs):
            pass

        def route(self, *_args, **_kwargs):
            return lambda fn: fn

    flask_stub.Flask = _Flask
    flask_stub.Response = Mock
    flask_stub.request = Mock()
    flask_stub.stream_with_context = lambda fn: fn
    sys.modules["flask"] = flask_stub

from hearth.backends import flm_tool_shim
from hearth.backends import lemonade_guard_shim


class UnloadBeforeLoadTests(unittest.TestCase):
    @patch.object(flm_tool_shim.fastflowlm, "load_model")
    @patch.object(flm_tool_shim.fastflowlm, "unload_model")
    @patch.object(flm_tool_shim.fastflowlm, "current_served_model", return_value="flm/old")
    @patch.object(flm_tool_shim.fastflowlm, "is_active", return_value=True)
    def test_opencode_fastflow_switch_records_active_without_service_handoff(self, _active, _current, unload, load):
        flm_tool_shim._ensure_fastflow_ready("flm/new")
        unload.assert_not_called()
        load.assert_not_called()

    @patch.object(lemonade_guard_shim.requests, "post")
    @patch.object(lemonade_guard_shim.requests, "get")
    def test_opencode_lemonade_switch_unloads_loaded_reasoning_model(self, get, post):
        response = Mock()
        response.json.return_value = {"all_models_loaded": [
            {"model_name": "old", "loaded": True, "type": "llm"},
            {"model_name": "embed", "loaded": True, "type": "embeddings"},
        ]}
        get.return_value = response
        post.return_value = Mock()

        lemonade_guard_shim._unload_before_implicit_load("new")

        post.assert_called_once_with(
            f"{lemonade_guard_shim.LEMONADE_BASE}/v1/unload",
            json={"model_name": "old"}, timeout=60,
        )


if __name__ == "__main__":
    unittest.main()
