# Playwright regression results — 2026-11-14

Credentials were read locally from `test-user`; no username, password, cookie, or token is stored in this report.

## Admin UI regression

- `tools/run_tests.py --browser`: 636 unit tests passed.
- All seven Playwright suites passed: first admin, admin controls, admin pages, agent model default, agents, model resource warnings, and model tests.

## Authenticated live app smoke

All routes returned successful responses and produced no browser console or HTTP errors:

`/`, `/models`, `/downloads`, `/metrics`, `/live-logs`, `/agents`, `/agents/new`, `/agents/schedules`, `/agents/skills`, `/quizzes`, `/settings`, `/users`, `/browse-history`, `/model-tests`, `/flm-converter`, `/dmv`.

## Downloads search

- Lemonade search rendered 73 cards; LM Studio search rendered 30 cards.
- Lemonade Nemotron and Muse Glimmer catalog checks passed.
- LM Studio LFM3 and Flint catalog checks passed.

The older smoke-script paths `/chat` and `/help` were removed from its page list: chat is `/`, and help is an API endpoint rather than a page.
