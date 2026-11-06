# q38rocm schema audit — 2026-11-04

## Verified model and engine

- Model: `Qwen3.8-27B-Q3_K_M.gguf`; GGUF architecture `qwen35`, file type 12
  (`Q3_K_M`), trained context 262,144 tokens.
- Artifact: 13,500,737,504 bytes; SHA-256
  `298edea3f8dc042dbf951014671843ac74a6f7e8cad2da8cbcf0650ae5414600`.
  The local checksum matches the published Hugging Face linked artifact checksum.
- Installed engine: build 11474, commit `75e67a92b2`.
- The GGUF template uses `<tool_call><function=...><parameter=...>` XML.
  The installed engine's `common/chat.cpp` explicitly recognizes this format.
- Live `/props` reports support for tools, parallel calls, object arguments,
  system messages, and reasoning effort. It reports no vision/audio/video support.
- Live `/apply-template` accepts tools with thinking both on and off. The rendered
  generation prompts respectively open `<think>` or include an empty closed
  thinking block, as specified by the GGUF template.

## Hearth and harness corrections

1. The installed OpenAI parser reads only string `tool_choice` values:
   `auto`, `none`, or `required`. Named function objects previously fell back to
   auto, and the earlier natural-language instruction did not enforce the name.
   A shared adapter now restricts offered tools to the selected schema and uses
   `required`. Unknown or ambiguous named choices fail before the request is sent.
2. The server reads `chat_template_kwargs.enable_thinking`. The shared adapter
   maps explicit top-level thinking booleans into that field. Chat keeps its saved
   model preference; compatibility tests default to thinking off.
3. Tests previously bypassed the chat adapter. Registry, direct harness, and CLI
   requests now use the same adaptation. UI tests resolve the live model ID.
4. Backend-specific output caps of 64, then 128/256 tokens could truncate valid
   workflow arguments. Tests now retain the fixture's declared budget (normally
   512 for ordinary checks, 2,048 for tool checks, 1,024 for large-code input).
5. Stream requests ask for usage events. Reported `timings` decode and prefill
   rates are preserved rather than treating total request duration as decode time.
6. The installed sampler does support frequency penalties. Hearth leaves the
   service default untouched unless the caller explicitly supplies a penalty.
7. q38rocm's saved verified compatibility recipe now applies to chat and tests
   using the live served ID. It does not force chat reasoning off.
8. The standalone CLI has a q38rocm backend option, uses its configured endpoint
   by default, and uses saved authentication only for that endpoint. Deliberate
   skipped probes no longer cause a failure exit code by themselves.
9. Two handoff unit tests omitted q38rocm mocks and could unload the real runtime.
   Their inventories are now isolated. The service was restored after this was found.

## Validation and remaining failure

53 adapter, streaming, harness, and API regression tests passed. Syntax and diff
checks passed. Both live template-rendering checks succeeded.

Two bounded direct generation probes were then checked with the corrected wire
schema. With thinking off, the server returned HTTP 200 but generated malformed
multilingual content instead of a required weather call, ending at 64 tokens.
With thinking on and low effort, it generated malformed reasoning and ended at
32 tokens. These are short diagnostic probes, not full compatibility evaluations.
Decode rates were about 2.77 and 2.76 tokens/sec respectively; prefill rates were
about 48.9 and 44.2 tokens/sec. No generation speed improvement is established.

At this point the artifact was intact and corrected requests were accepted, but
generation was still malformed. The follow-up below isolates the faulty setting.

The earlier 135-check run remains interrupted: 88 results, including 8 passes,
35 failures, and 45 dependent skips. It was not restarted during this audit.
q38rocm retains the cache-enabled speed profile, Vulkan acceleration, disabled
MTP, and configurable reasoning. Prompt caching can improve repeated prefill;
the measured decode rates above should not be presented as accelerated inference.

## Follow-up: Flash Attention isolated and disabled

The same engine and artifact were tested in isolated servers at 4K context with
the same required weather prompt. No templates, weights, or expected arguments
were changed between configurations.

| Execution | KV cache | Flash Attention | Weather result | Decode rate |
|---|---|---|---|---|
| CPU | FP16/FP16 | Off | Correct Boston call, 26 tokens | 2.46 tok/s |
| Vulkan | Q8/Q8 | On | Incorrect content, stopped at 64 tokens | 2.77 tok/s |
| Vulkan | FP16/FP16 | On | Malformed multilingual text, stopped at 64 tokens | 2.78 tok/s |
| Vulkan | FP16/FP16 | Off | Correct Boston call, 26 tokens | 2.76 tok/s |
| Vulkan, optimizations disabled | FP16/FP16 | Off | Correct Boston call, 26 tokens | 2.76 tok/s |
| Vulkan | Q8/FP16 | Off | Correct Boston call, 26 tokens | 2.47 tok/s |

Changing only Flash Attention in the FP16 comparison switches failure to success.
This establishes a Flash Attention correctness problem in this installed GPU
configuration, rather than damaged weights or a need to disable GPU acceleration.
It does not identify the faulty shader instruction or establish a driver-wide bug.

The managed service now uses FP16 KV and explicitly overrides the launcher's
Flash Attention default to off. The override appears after the model path because
the upstream launcher's generic flag parser otherwise treats its `off` value as
a positional model path. A 262K context with the corrected FP16 cache exceeded
the GPU allocation limit and caused Vulkan device-loss failures. The installed
default is therefore 128K with approximately 8 GiB of attention KV, physical
microbatches of 64, and prompt checkpoints bounded to 2 GiB. The model's maximum
remains 262K; `Q38ROCM_CONTEXT` can override the service installer's window on
hardware with sufficient memory. Hearth reports the live loaded context.
MTP remains disabled and per-request reasoning remains configurable.

This is a correctness fix. Raw decode speed is essentially unchanged, while the
successful weather call finishes in about 15 seconds instead of wasting 28 seconds
on a truncated, incorrect response.
