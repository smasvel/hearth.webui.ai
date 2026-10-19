# Test repairs — October 18, 2026

Reviewed the latest saved matrix, `85de98f13cd94e34be9bbcc1819c4ec3`:
25 models, 1,795 assertions, 1,080 passed and 715 failed. Of the failures,
417 were dependent steps skipped after an earlier failure. Nine models failed setup.
The historical report was preserved.

## Implemented fixes

- Restore recovery of XML-wrapped JSON with a string `function` field; this was
  the sole failing local unit test.
- Decode Gemma native tool markers with nested, typed arguments and preserve
  literal strings. Recover Llama XML parameters only when the offered schema
  identifies a single unambiguous parameter.
- Preserve document order across mixed XML/JSON tool markup and avoid interpreting
  overlapping blocks twice. Reject mixed arrays containing non-call entries.
- Apply textual tool recovery at LM Studio's actual validation/proxy endpoint,
  in both ordinary and streaming responses. Preserve streamed token usage.
  Requests with tools buffer their response to permit recovery; ordinary text
  requests still stream immediately.
- Enable automatic tool calling and Hermes parsing for the installed vLLM
  Qwen3 CPU service. The installed drop-in is reproduced in
  `deploy/vllm-tools.conf`; it is specific to that local Qwen3 service.
- Validate named tool calls returned with vLLM's `stop` finish reason. Actual
  function names and arguments must still match; automatic and required choices
  still require `tool_calls`, and truncated responses still fail.
- Disable hidden reasoning for tools-bearing validation requests, consistent
  with the local coding request policy. Preserve ordinary chat reasoning.
- Stop issuing further network requests within a model suite after a timeout or
  connection failure. Remaining assertions stay failed/blocked. Skip prompt-recipe
  repair when the backend itself is unavailable. Include HTTP error response bodies
  in future diagnostics.

## Verification

` .venv/bin/python -m unittest discover -s tests `: **211 passed**.

**vLLM core live checks: 26/26 passed**, using the final configuration and harness.

Live validation artifacts are in `data/vllm-core-final.json`,
`data/lmstudio-repair-validation.json`, and `data/lmstudio-repair-candidate.json`.
The LM Studio baseline was deliberately stopped after reproducing remaining
model-behavior failures; it is not a complete matrix rerun.

## Remaining work

The complete 25-model matrix has **not** been verified clean. The small LM Studio
distilled model still generated duplicate calls and simulated shell/file execution
instead of calling the offered function during the baseline. Such responses remain
failures; the proxy does not invent calls or execution results to satisfy assertions.

Historical failures on other targets include workflow instruction-following errors,
Gemma template role errors, model loading/health failures, and FLM runtime timeouts.
The strict-profile candidate corrected the duplicate typed call but still simulated
a ping result instead of invoking the function, so it was not persisted.

Those other targets require further live investigation; this report does not claim that
their failures were resolved. No compatibility recipe is persisted on the basis of
an incomplete or regressing candidate run.
