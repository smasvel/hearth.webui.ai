# Repository guidance

- For every feature or behavior change, update both `README.md` and
  `deploy/FIRST_TIME_INSTALL.md` with the user-facing behavior and any setup or
  operational steps. Treat these documentation updates as part of completing the
  change, not as optional follow-up work.
- Keep README feature summaries aligned with the actual UI and supported behavior.
- Do not use financial market, stock, trading, ticker, or investment examples in
  agent UI copy or agent workflow examples.
- Before every push, inspect all files and history being pushed for credentials,
  private keys, session secrets, and private infrastructure identifiers such as
  personal hostnames, tailnet DNS names, and private IP addresses. Replace them
  with placeholders or remove them before pushing; never publish real secrets.
