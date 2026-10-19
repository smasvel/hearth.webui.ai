import unittest
from unittest.mock import patch

import app


class StartupModelTests(unittest.TestCase):
    def test_single_model_session_skips_support_warmup(self):
        with (
            patch.object(app.settings_store, "get", return_value="false"),
            patch.object(app, "_ensure_embedding_model_loaded") as embedding,
            patch.object(app.lemonade, "_ensure_voice_model_loaded") as voice,
        ):
            app._warm_up_rag_and_voice_models()
            embedding.assert_not_called()
            voice.assert_not_called()

    def test_restart_preserves_loaded_lmstudio_instance(self):
        model = "qwen/qwen3.5-4b"
        with (
            patch.object(app.settings_store, "get", return_value="lmstudio/" + model),
            patch.object(app.lms, "owns_model", return_value=True),
            patch.object(app.lms, "list_models", return_value=[{"id": model, "state": "loaded"}]),
            patch.object(app, "_unload_reasoning_models_before_load") as handoff,
            patch.object(app.lms, "load_model") as load,
        ):
            app._restore_default_model()
            handoff.assert_called_once_with(model)
            load.assert_not_called()

    def test_cold_start_loads_lmstudio_with_chat_context_and_gpu(self):
        model = "qwen/qwen3.5-4b"
        with (
            patch.object(app.settings_store, "get", return_value="lmstudio/" + model),
            patch.object(app.lms, "owns_model", return_value=True),
            patch.object(app.lms, "list_models", return_value=[{"id": model, "state": "not-loaded"}]),
            patch.object(app, "_unload_reasoning_models_before_load"),
            patch.object(app.lms, "load_model") as load,
        ):
            app._restore_default_model()
        load.assert_called_once_with(model, context_length=32768)


if __name__ == "__main__":
    unittest.main()
