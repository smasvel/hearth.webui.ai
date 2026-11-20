import unittest
from unittest.mock import patch

import app


class StartupModelTests(unittest.TestCase):
    def test_lemonade_npu_reuses_warm_model_without_context_override(self):
        model = "qwen-test-FLM"
        with (
            patch.object(app.npu_request_lock, "acquire") as acquire,
            patch.object(app, "_free_npu_except"),
            patch.object(app, "_saved_model_context", return_value=None),
            patch.object(app.lemonade, "is_npu_model", return_value=True),
            patch.object(app.lemonade, "list_models", return_value=[
                {"id": model, "state": "loaded", "loaded_context_length": 57343},
            ]),
            patch.object(app.lemonade, "load_model") as load,
        ):
            handle = app._claim_selected_npu(app.lemonade, model)
        self.assertIs(handle, acquire.return_value)
        load.assert_not_called()

    def test_startup_does_not_restore_kobold_default(self):
        with patch.object(app.settings_store, "get", return_value="kobold/koboldcpp/example.gguf"), \
             patch.object(app.kobold, "owns_model") as owns, \
             patch.object(app.kobold, "load_model") as load:
            app._restore_default_model()
        owns.assert_not_called()
        load.assert_not_called()

    def test_startup_restores_default_image_model(self):
        with (
            patch.object(app, "_ensure_embedding_model_loaded"),
            patch.object(app.lemonade, "_ensure_voice_model_loaded"),
            patch.object(app.lemonade, "_ensure_image_model_loaded") as image,
        ):
            app._warm_up_startup_models()
        image.assert_called_once_with(app.lemonade.IMAGE_MODEL_ID)

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
