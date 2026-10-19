# Granite tool-test repairs — October 18, 2026

Baseline: `73c431d9aa1d43198702f0afcb3d9488`, Granite 4.0 H Tiny on
Lemonade. Latest-attempt results were 71 passed, 10 failed, and 30 blocked,
out of 111 distinct checks. Historical results are preserved.

## Changes

- Add a Granite 4 compatibility transport in the Lemonade proxy. The embedded
  template supports `available_tools`; passing the offered functions through that
  template input avoids llama.cpp's automatically inferred tool-argument grammar.
  A direct comparison showed that unconstrained generation retained requested
  fields such as `multiple: true`, while the native chat tool path omitted them.
- Request object-valued arguments and complete native tool tags. Decode only
  complete JSON calls to functions actually offered by the client. JSON-encoded
  argument objects are decoded without adding missing fields or changing values.
  Normal and streamed replies retain upstream metadata and usage. Truncated,
  malformed, unknown-function, and incomplete streams remain unconverted.
  The existing JSON decoder handles redundant terminal closing delimiters after
  a complete object; it does not repair truncated strings or invent fields.
- Preserve required/named tool-choice instructions. Plain chat, `tool_choice:
  none`, other model families, and multiple-completion requests retain their
  existing transport.
- Validate the verified passing-test count rather than the exact phrase `4 passed`.
  Equivalent wording such as `4 tests passed` now passes; wrong counts, negated
  claims, nonzero failures, extra calls, and missing tool arguments still fail.
- Tool-call failures now show both expected and received arguments.
- Remove conflicting shared prompt wording that hard-coded `filePath` for edit
  calls even when the offered schema declares `path`. Both forms now follow the
  actual tool schema. The `path` edit case passed in both response modes in a
  targeted live check before final activation.

## Validation

240 automated tests passed. Both existing browser suites passed during this work.
Regression tests cover typed native calls, both response modes, retained usage,
unique call IDs, malformed and truncated data, unsupported tool names, choice
instructions, and equivalent versus incorrect test-count statements.

The final live app validation report is `f19fb2018e9844daa5aef1099cc25eaf`.
The app and proxy were activated only after confirming no active saved test or
agent jobs. No services are restarted during the validation run.
The preliminary run `3954a258196640b898a359ea441e168e` exposed the path-key
conflict and was cancelled after its current request completed, before the final
correction was activated. It remains available as a diagnostic report.
The next candidate, `c2c690ef5177402082d04dc1a23d8235`, exposed a redundant
terminal delimiter in a streamed typed call. It was also cancelled after its
current request completed before the existing tolerant decoder was connected.

## References

- [IBM Granite model's native tool format](https://huggingface.co/ibm-granite/granite-4.0-h-tiny)
- [Installed llama.cpp revision's chat request conversion](https://github.com/ggml-org/llama.cpp/blob/dbe4c3ed4/tools/server/server-common.cpp)
