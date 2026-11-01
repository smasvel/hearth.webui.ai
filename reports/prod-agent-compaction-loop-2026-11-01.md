# Agent compaction floor fix (Muse model)

Repeated large-file reads in the Muse OpenCode agent run kept bouncing after the
conversation auto-compacted. The shared OpenCode config used to cap its retained
recent-turn budget to the *smallest* session model's window, so when Muse was the
selected model its own larger context was still handed an under-sized history and
the same read was retried after each compaction.

## Guard against repeating the fix

`agent_container.ContainerOpenCode._agent_config()` now treats the selected
agent model's own context window as the compaction floor. Lemonade/Muse-style
65K+ models keep their full 65K recent-context budget with an 8K output reserve;
smaller sizes still scale to one eighth of window. That keeps a large file read
Recent enough to avoid being lost after the summarization step.

## Misc

- Added unit tests covering the floor behavior across Muse-sized Lemonade
  contexts, smaller sibling models, an oversized buffer, and the actual
  `compaction.failed` recovery path for a Lemonade/Muse assignment.

## What was tested

- `tests.test_agent_opencode` (with a dedicated regression test for the larger
  selected model) and the generated help-config tests all pass.
- README and first-time install docs mention the new floor so a larger-window
  assignment does not inherit a smaller sibling's compaction settings.
