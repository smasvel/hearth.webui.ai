# PostgreSQL deployment and SQLite migration

Hearth's database code uses PostgreSQL 18. The Compose service is defined in
`compose.yaml`, publishes only `127.0.0.1:55432`, uses a named volume, and has a
readiness health check. The application and proxies continue running as their
existing user systemd services; they connect through `DATABASE_URL` in `.env`.
There is no runtime SQLite fallback.

For a new installation without SQLite data, use
[the first-time installation guide](deploy/FIRST_TIME_INSTALL.md).

## Current migration status

**Live cutover completed on October 18, 2026.** Hearth and all three proxies now
use the healthy PostgreSQL 18 Compose service through `DATABASE_URL`. The login page
and authenticated users, conversations, browse-history, metrics, and recent-metrics
API routes all returned HTTP 200 after restart.

The final migration copied and checksum-verified **13,080 rows across all
10 tables**. All four project database files were inspected; three were empty.
Original SQLite files remain intact. Consistent source snapshots and the previous
application environment are retained under `data/sqlite-backups/`. The final report
is `data/postgres-migration.json`. A verified PostgreSQL dump is saved at
`data/postgres-backups/after-cutover-2026-10-18.dump`.

Validation: **230 unit/integration tests pass** against PostgreSQL, including the
first-time installer checks. Both Playwright browser suites passed after the cutover.
Fresh admin creation and credential preservation on installer reruns were also
verified in a separate Compose database. The Compose configuration validates successfully.

## Cutover procedure (for an existing SQLite deployment)

Install Docker Engine and Compose, start Docker, and grant the account access:

```bash
sudo apt-get install -y docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo usermod -aG docker "$(id -un)"
```

Start a new login session to pick up the Docker group, or grant the current account
access to the running daemon's socket (this host has no `sg`/`newgrp` command):

```bash
sudo setfacl -m "u:$(id -un):rw" /var/run/docker.sock
```

From this project directory, run the prepared cutover:

```bash
.venv/bin/python tools/cutover_postgres.py
```

The cutover command:

1. Starts PostgreSQL with `docker compose --env-file .env.postgres up -d --wait`.
2. Refuses a populated destination before stopping any application services.
3. Stops the running web app and three Hearth proxies to freeze SQLite writes.
4. Creates consistent backups of every `.db`, `.sqlite`, and `.sqlite3` file,
   excluding virtual environments and earlier backups.
5. Copies every application row in one PostgreSQL transaction, including binary
   embeddings, original IDs, password hashes, API settings, tokens, and timestamps.
6. Verifies row counts and SHA-256 digests of every row in every source table,
   and advances PostgreSQL sequences beyond both existing IDs and SQLite sequences.
7. Saves the old `.env`, switches `DATABASE_URL`, restarts the previously active
   services, and checks that Hearth's login page responds.

`.env.postgres` has already been generated with a random password and mode `600`.
It is ignored by Git. The cutover leaves the existing `.env` unchanged until the
migration succeeds. Its report is `data/postgres-migration.json`.

The source database contains historical messages without parent conversations.
These records are preserved exactly. The corresponding PostgreSQL foreign key is
installed `NOT VALID`: PostgreSQL enforces it for future writes while retaining
legacy orphans. Unvalidated constraints are listed in the migration report.
No parent conversations are invented and no orphan messages are deleted.

If a migration step fails, its transaction rolls back and the SQLite originals and
snapshots remain. If service startup fails after switching, the command reports
that failure and retains both copies. It never silently switches back after
PostgreSQL may have accepted new writes. Inspect the service log before retrying;
a populated PostgreSQL target is intentionally protected against a second import.

## Development and tests

Install the requirements using the environment's interpreter:

```bash
.venv/bin/python -m pip install -r requirements.txt
```

Set `DATABASE_URL` to an accessible PostgreSQL database owned by the application
role. Use the isolated runner, which creates and drops a separate schema:

```bash
.venv/bin/python tools/run_tests.py --browser
```

The cutover command intentionally refuses to re-import into the current populated
database. Do not rerun it against the live database.

The role must be allowed to create schemas for this test command. Avoid running
individual browser scripts or importing the application against production tables
when testing; the isolated runner supplies the temporary schema to subprocesses.

To migrate manually into an empty database, stop all writers first and run:

```bash
.venv/bin/python tools/migrate_sqlite_to_postgres.py
```

Use repeated `--source PATH` options for explicit SQLite sources. `--verify-only`
compares a source or saved snapshot to PostgreSQL without modifying either. Runtime
trace retention can change records after Hearth starts, so perform exact post-import
verification before restarting writers. Unknown source tables, conflicting IDs,
nonempty destination tables, and content mismatches cause an error.

## Backups

Keep the SQLite backups until the cutover has been accepted. Back up PostgreSQL
with `pg_dump`; for example:

```bash
umask 077
docker compose --env-file .env.postgres exec -T postgres \
  pg_dump -U hearth -d hearth -Fc > hearth-postgres.dump
```

Do not use `docker compose down -v` unless intentionally deleting the database
volume. The named volume survives ordinary container replacements and restarts.
