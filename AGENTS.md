# Repository guidance

- Keep `README.md` focused on features, supported behavior, and setup. Never add
  bug-fix notes or defect history to the README.
- Update `deploy/FIRST_TIME_INSTALL.md` for changes to user-facing behavior,
  setup, or operational steps. Update README feature summaries when the set of
  supported features changes.
- Do not use financial market, stock, trading, ticker, or investment examples in
  agent UI copy or agent workflow examples.
- Before every push, inspect all files and history being pushed for credentials,
  private keys, session secrets, and private infrastructure identifiers such as
  personal hostnames, tailnet DNS names, and private IP addresses. Replace them
  with placeholders or remove them before pushing; never publish real secrets.
