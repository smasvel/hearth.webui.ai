# Agent compaction floor fix (Muse model)

Repeated large-file reads in the Muse OpenCode agent run kept bouncing after the
conversation auto-compacted. The shared OpenCode config used to cap its retained
recent-turn budget to the *smallest* session model's window, so when Muse was the
selected model its own larger context was still handed an under-sized history and
the same read was retried after each compaction.

## Guard against repeating the fix

`agent_container.ContainerOpenCode._agent_config()` now treats the selected
agent model's own context window as the compaction floor. Lemonade/Muse-style
65K+ models keep a large recent-context budget with an 8K output reserve;
smaller sizes still scale to one eighth of window. That keeps a large file read
Recent enough to avoid being lost after the summarization step.

## Follow-up regression found in live logs (2026-11-02)

`keep.tokens` was capped to the model's *whole* window (65,536 on a 65,536-token
window). OpenCode's auto-compaction must itself send a summarization request that
fits inside the window, so once the session filled it, OpenCode could not shrink
the request below the keep floor and failed with "The compaction request cannot
be reduced further without losing the latest exchange or checkpoint"
(`~/.local/share/opencode/log/opencode.log` around 2026-11-02T01:42Z, Muse
provider session). Fix: `keep + buffer + prompt overhead` must stay inside the
window. `_agent_config()` and `help_data.build_opencode_config()` now derive a
65K+ floor as `min(65536, window - buffer - max(4096, window // 8))` — 49,152
keep / 8,192 buffer on a 65,536-token window — and clamp any pre-existing
oversized keep value down to that safe maximum. The desktop
`~/.config/opencode/opencode.jsonc` was patched to the same values, tests updated,
and README/install docs now document the reserve room.

## Misc

- Added unit tests covering the floor behavior across Muse-sized Lemonade
  contexts, smaller sibling models, an oversized buffer, and the actual
  `compaction.failed` recovery path for a Lemonade/Muse assignment.

## What was tested

- `tests.test_agent_opencode` (with a dedicated regression test for the larger
  selected model) and the generated help-config tests all pass.
- README and first-time install docs mention the new floor so a larger-window
  assignment does not inherit a smaller sibling's compaction settings.

## Full automation (2026-11-02)

The keep/buffer pair is no longer hand-edited anywhere. The formula lives in
`help_data.compaction_for_window(window, output_reserve)` and both config
generators consume it. `tools/sync_opencode_config.py` regenerates
`~/.config/opencode/opencode.jsonc` from live backend state, preserving the
selected default model and hand-added agents and re-deriving compaction from
the selected model's current advertised window. The first-time installer now
installs it as the `opencode-config-sync.timer` systemd user timer (every two
minutes); the run is idempotent and leaves a timestamped `.bak` on real changes.
