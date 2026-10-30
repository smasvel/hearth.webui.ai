# First-time installation Docker verification — 2026-10-30

Tested Ubuntu 24.04 amd64 with Python 3.12, real package downloads, real Docker
Compose PostgreSQL 18 databases, and no existing Hearth environment files.
The host application/database were not used as test data. Separate Compose projects
`hearth-install-check` / `hearth-release-check` used ports 55587 / 55588.

## Reproduced and fixed

- Closed standard input raised EOFError at optional runtime selection. It now skips
  the optional install. Explicit flags remain available.
- Release setup could not skip its API key prompt. `--no-api-key` skips it and
  preserves an existing key; EOF and blank input also preserve an existing key.
- Release child argument forwarding duplicated `--backend=...` and
  `--backend-url=...` choices. Both equals and separate-value forms are recognized.
- LM Studio's script endpoint returned HTTP 403 to Python urllib in this test.
  Linux/macOS now download using curl, matching the vendor's documented method.
- A clean Ubuntu image lacked libatomic1 and LM Studio installation stopped.
  Optional LM Studio setup installs that prerequisite and curl/certificates when needed.
- Current Lemonade packages provide lemond, not lemonade-server. Existing-install
  detection recognizes both and avoids repeating package installation.
- A downloaded Chromium executable failed with missing libnspr4.so. Linux browser
  setup now uses Playwright's --with-deps option to install its required OS libraries.
- Installing a missing base dependency tried systemctl even with a working external
  Docker socket. A working Docker daemon is now reused. Missing systemd produces
  actionable guidance; use --no-service for the first-time installer in a container.
- First-time shell setup now rejects Python older than 3.12 explicitly. Release
  setup validates database ports and offers --project-name for isolated installations.

## Successful checks

- `deploy/install-first-time.sh --no-service --skip-backend-install` completed
  with an isolated database, both with and without browser installation.
- Removed acl, then reran the shell installer: missing dependency installed;
  accessible Docker socket reused without requiring systemd.
- Fresh `deploy/install-release.sh --backend=lmstudio --skip-browser --skip-agent-image
  --no-api-key --postgres-port 55588 --project-name hearth-release-check` completed
  with closed stdin and its own virtual environment/database.
- Real Lemonade stable PPA package installed (2026.44.1~24.04); lemond served
  `/api/v1/models` with HTTP 200 on test port 13487.
- Real LM Studio llmster 0.0.27-1 installed. In a separate network-isolated runtime
  container, `lms daemon up`, `lms server start --port 1234`, and `/v1/models` succeeded.
  The first attempt in the host-network setup container encountered the existing host
  LM Studio daemon's authentication, so runtime startup verification used bridge networking.
- Hearth's first administrator setup page returned HTTP 200 with its confirmation field.
- Chromium launched headlessly and rendered a local HTML page after dependency repair.
- Repeat setup preserved environment file hashes, database credentials, the session
  signing key, and a test administrator account. A saved test API key was also checked.
- All 26 relevant unit tests passed inside the container; shell syntax and git whitespace checks passed.

## Reproduction notes and limits

Build the base test image with `docker build -t hearth-install-check:local tests/install`. Copy a clean source tree including `.env.example`
into `/home/tester/hearth`, owned by tester; exclude real `.env`, `.env.postgres`,
account credential files, data, and virtual environments. The setup container needs
an accessible Docker socket, its socket group added to tester, and host networking
so the installer can reach its loopback-published test database. Always supply a
unique Compose project and unused port. Runtime tests should use a separate bridge
network container to avoid existing host runtime ports. Tear down only test projects
and their volumes afterwards.

Normal Docker containers do not exercise systemd boot startup, Windows/macOS
installers, GPU/NPU drivers, or model inference. The existing agent image was reused;
this check did not rebuild it. Python dependencies, PostgreSQL, Chromium, and runtime
packages were actually installed; unit mocks were used only for additional edge cases.

Both test setup containers, their test PostgreSQL containers, and the two test database
volumes/networks were removed after verification. The reusable test base image remains.

## Second run: deleted image and rebuilt without cache

At the user's request, deleted `hearth-install-check:local` and rebuilt with
`--no-cache`. The new image ID was `e66ac4a4a4f5`. Created new containers
`hearth-fresh-first` and `hearth-fresh-runtime`, copied clean current source only,
and created new PostgreSQL volumes for projects `hearth-fresh-first` (55589) and
`hearth-fresh-release` (55590). No previous virtual environments, environment files,
browser caches, or LM Studio files were copied into these containers.

Both shell installers completed successfully. The first-time run included Chromium
with OS dependencies and Lemonade installation; removing acl beforehand exercised
missing-host-dependency recovery again. The release run used a separate fresh source
directory, virtual environment, and database, with closed stdin, `--backend=lmstudio`,
`--skip-browser`, `--skip-agent-image`, and `--no-api-key`.

The independent runtime container downloaded LM Studio from scratch, automatically
installed libatomic1, started llmster and the API, returned HTTP 200 from `/v1/models`,
and successfully reused the installation on a second invocation. Lemonade returned
HTTP 200 from `/api/v1/models` on test port 13489. Chromium launched and rendered HTML.
Both installations showed the first-administrator form. Repeating setup preserved the
test administrator, environment files, session key, and API key. All 26 regression
tests passed again. No new installer failures were found.

Test containers and their PostgreSQL volumes/networks were removed after the second
run. Raw installation logs are available in `/tmp/hearth-fresh-retest/` for this host
session. The existing agent image was reused; systemd boot startup, Windows/macOS,
and GPU/NPU inference were not exercised.

## Final cleanup and installer review

At the user's request, removed the temporary Docker Hearth deployment on port 5444,
its three volumes, network, Docker image, install-check image, generated `.env.docker`,
relay directory, and temporary installation-log/source directories. Removed only the
new Tailscale Serve route on 8448; the original host route and host database/agents
were preserved. Previous `/tmp` log references above describe historical runs; those
temporary files have now been removed.

The final script review moved argument validation before runtime selection, added the
legacy SQLite-data guard to release setup, moved the guard before runtime/image
installation, added early shell guidance for hosts without systemd, and changed service
reruns to restart the existing service so new code and renewed certificates take effect.
Regression tests now isolate storage initialization from the live host database. All
37 relevant tests passed, along with shell syntax and whitespace checks. No additional
test containers were created during this final review.
