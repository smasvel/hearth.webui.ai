import json
import unittest
from unittest.mock import patch

import app


class PromptBackend:
    def __init__(self):
        self.messages = None

    def chat_stream(self, messages, model, temperature=0.7, max_tokens=None):
        self.messages = messages
        response = {
            "choices": [{"delta": {"content": json.dumps({
                "prompt": "Lord Murugan, youthful Hindu deity riding a peacock, holding a vel",
                "negative_prompt": "unrelated subject",
            })}}]
        }
        yield "data: " + json.dumps(response)
        yield "data: [DONE]"


class ThinkingPromptBackend(PromptBackend):
    def chat_stream(self, messages, model, temperature=0.7, max_tokens=None, enable_thinking=True):
        self.enable_thinking = enable_thinking
        yield from super().chat_stream(messages, model, temperature, max_tokens)


class FailedPromptBackend:
    def chat_stream(self, messages, model, temperature=0.7, max_tokens=None):
        yield 'data: {"error":{"message":"vk::Queue::submit: ErrorDeviceLost"}}'
        yield "data: [DONE]"


class ImagePromptTests(unittest.TestCase):
    def test_contextual_image_prompt_works_with_opencode_chat_signature(self):
        backend = PromptBackend()
        context = [
            {"role": "assistant", "content": (
                "Lord Murugan, also known as Kartikeya, is traditionally depicted "
                "with a peacock mount and a vel spear."
            )}
        ]

        result = app._enhance_image_prompt(
            "Create an image using the description above",
            context,
            "opencode/beam-free",
            backend,
        )

        self.assertIn("Lord Murugan", result["prompt"])
        self.assertIn("peacock", result["prompt"])
        self.assertIn("vel spear", backend.messages[1]["content"])

    def test_prompt_shaping_disables_thinking_when_backend_supports_it(self):
        backend = ThinkingPromptBackend()
        app._enhance_image_prompt("Create a devotional image of Murugan", [], "local-model", backend)
        self.assertFalse(backend.enable_thinking)

    def test_backend_error_uses_iconography_fallback_instead_of_bare_prompt(self):
        result = app._enhance_image_prompt(
            "Generate an beautiful high 4k photo Hindhu god Murugan",
            [],
            "local-model",
            FailedPromptBackend(),
        )
        self.assertIn("peacock mount clearly visible", result["prompt"])
        self.assertIn("Vel spear", result["prompt"])

    def test_generic_fallback_preserves_requested_subject_and_attributes(self):
        result = app._fallback_image_prompt("A red fox beside a blue cabin at night")
        self.assertIn("red fox beside a blue cabin at night", result["prompt"])
        self.assertIn("Do not substitute the subject", result["prompt"])

    def test_evicted_lemonade_model_is_not_loaded_for_prompt_enhancement(self):
        with patch.object(app.lemonade, "is_model_loaded", return_value=False), \
                patch.object(app.lemonade, "chat_stream") as chat_stream:
            result = app._enhance_image_prompt(
                "Generate an beautiful high 4k photo Hindhu god Murugan",
                [],
                "PhillyLabs-KAT-Coder-V2.5-Dev-35B-A3B-ABLITERATED-UNCENSORED-PHILADELPHIA-CLASS",
                app.lemonade,
            )
        chat_stream.assert_not_called()
        self.assertIn("peacock mount clearly visible", result["prompt"])

if __name__ == "__main__":
    unittest.main()
