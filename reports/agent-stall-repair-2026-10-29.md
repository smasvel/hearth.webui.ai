# Agent stall repair — October 29, 2026

Reviewed the Hearth service journal, persisted assignment activity, model relay
traces, and the running OpenCode session APIs.

## Observed failures

- The service-log summarizer assignment reached 20 work cycles, then remained
  marked as running while OpenCode's active-session list was empty. An inbox
  prompt and an unfinished, empty assistant message remained. Hearth waited for
  its former 60-minute first-token deadline before marking the task blocked.
- Relay traces repeatedly reported broken pipes after roughly five minutes.
  The relay read SSE in 64 KiB chunks, which could withhold small streamed
  responses from close-delimited upstream connections.
- Completed OpenCode tool steps could be reconciled as finished agent turns,
  allowing redundant continuation prompts to enter an active runner's inbox.
- Fresh-session compaction recovery cleared the session reference before forcing
  a workspace checkpoint and omitted the original goal from its new prompt.
- The per-assignment thinking choice was saved but was not passed to container
  configuration. Retrying with thinking disabled could still enable thinking.

## Changes

An abandoned model/tool handoff now triggers recovery after 90 seconds. Real
requests have a 10-minute first-token limit and a five-minute progress limit.
Current progress is read before timeout decisions, including for idle sessions.
Relay progress is associated with a task ID, and tool-call deltas count as output.
SSE lines are forwarded as they arrive while preserving their original framing.

Recovery interrupts the old runner, forces a current checkpoint, and retires its
container before creating a fresh session with the original goal, criteria, and
refinements. It preserves the work-cycle count and allows two session recoveries.
Three consecutive responses without tool calls also trigger recovery. Transport
retries are bounded, and exhausted tasks expose a review state.

Completed tool steps are distinguished from completed agent turns. Thinking
choices now reach container configuration and take effect when resuming a task
with a different saved setting. Container reuse preserves ownership of the
workspace root directory. README and installation guidance describe the new limits.

## Validation

- `tools/run_tests.py`: **371 tests passed**, using an isolated PostgreSQL schema.
- Real HTTP relay test: small SSE tool deltas reached the client before the
  upstream response finished; original CRLF framing was preserved.
- Real Docker/OpenCode run with controlled OpenAI-compatible responses: wrote a
  file, ran a shell check, and passed Hearth's independent verification in **one
  work cycle**.
- Real Docker recovery run: forced a checkpoint, replaced the OpenCode session,
  restored the file, and passed independent verification with the work-cycle
  count still **one**. Temporary containers and schemas were removed.
- `git diff --check` passed.

## Remaining model limitation

The loaded `qwen3.8-2b-distill` model was also tested against a one-file task.
Requests offered ten actual tools and correctly carried `enable_thinking: false`.
It repeatedly returned implementation text without tool calls. Hearth detected
that behavior, retried fresh sessions, and stopped at the check's five-cycle
budget. Infrastructure validation with controlled responses establishes lifecycle
correctness; it does not establish this model's ability to complete project tasks.

Existing production assignments and their statuses were preserved. Continue a
blocked/interrupted assignment from its task page; model behavior can still
prevent successful completion.
