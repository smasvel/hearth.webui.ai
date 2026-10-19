# Devstral Small 2507 tool template

Adapted from llama.cpp's `models/templates/unsloth-mistral-Devstral-Small-2507.jinja`:
https://github.com/ggml-org/llama.cpp/blob/master/models/templates/unsloth-mistral-Devstral-Small-2507.jinja

Copyright 2025-present the Unsloth team. Licensed under Apache License 2.0 (see LICENSE).

The GGUF bundled template rejects tool roles. This version retains the upstream
`[AVAILABLE_TOOLS]`, `[TOOL_CALLS]`, `[ARGS]`, and `[TOOL_RESULTS]` format, handles
null assistant content, replaces the long default system prompt with a short default,
and normalizes quotes/whitespace for Lemonade's CLI argument parser.

`lemonade_templates.load_options` passes actual Jinja using `--chat-template` at
model load time. Per-request `chat_template` is ignored by the installed backend;
with `--jinja`, a built-in template name is interpreted as literal Jinja text.
Inlining avoids the snap daemon's inability to read files in the web UI home.
The override applies only to Devstral-Small-2507-GGUF and preserves current context
and backend arguments. Existing explicit user template overrides take precedence.
