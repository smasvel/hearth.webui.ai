# Question workflow test repairs — October 18, 2026

The latest saved Nemotron run (`0eef04ad179a40dba66adf2410bf8a10`)
failed batched questions because the model reversed the supplied interface choices,
and failed dismissed-question continuations because it resumed older project work.
Both failures were reproduced through the live Lemonade proxy before changes.

Changes:

- Clarify the shared agent request policy: the latest requested step defines the
  response scope, and an explicitly requested text reply can complete the turn
  while older project work remains unfinished.
- Restore explicitly supplied question option order only when question metadata
  and the complete multiset of choices already match. Changed descriptions,
  missing choices, changed selection modes, and ambiguous input remain untouched.
  Both ordinary completions and fragmented SSE retain original call IDs.
- Add regression coverage for ordinary and streamed order repair, mismatched
  choices, ambiguous payloads, and ignoring earlier user turns.

The Lemonade guard service was restarted to activate the changes. Historical
model-test runs remain intact. The full multi-model matrix was not rerun.

Validation artifacts:

- `data/nemotron-question-repair-baseline.json`
- `data/nemotron-question-repair-validation.json`

Automated validation: ` .venv/bin/python tools/run_tests.py --browser `:
232 tests passed, and both browser suites passed.

Live validation: 16/16 Nemotron checks passed through the restarted proxy:
single-choice, multiple-choice, batched questions, dismissed questions, and their
continuations, each with ordinary and streamed responses. The identical batched
and dismissed workflows passed only 2/8 checks in the reproduced baseline.
