# Install verification notes (2026-11-01)

## Scope

- Removed only the Hearth Compose stack (`hearth-postgres-1`, `hearth_postgres_data`, `hearth_default`).
- Left the unrelated `rf-signals` stack untouched.
- Reinstalled through the repo scripts and verified the parallel backend services.

## Initial failures found

1. **`deploy/install-docker.sh` exited with `tailscale_port: unbound variable`.**
   The optional Tailscale HTTPS flag was parsed into a variable that was never
   initialized, so every rerun failed under `set -u` even when `--tailscale-https-port`
   was intentionally omitted.
   Fix: `tailscale_port=0` is now initialized before flags are parsed.

2. **`deploy/install-first-time.sh` demanded the Ubuntu `python3-venv` apt package even
   though `.venv` was already usable.**
   It treated any absent `dpkg-query` package file as a missing host dependency, which
   blocked noninteractive reruns because sudo had no prompt available.
   Fix: the apt package is only required when `.venv/bin/python` does not already exist,
   and the venv is now created only when it is missing.

## Result after fixes

- `bash deploy/install-first-time.sh --skip-browser --no-service --skip-backend-install`
  reused `.venv`, reused requirements, recreated `/hearth_postgres_data`, and reported a
  fresh database with no users yet.
- `bash deploy/install-docker.sh` rebuilt the Docker Hearth image and recreated
  `hearth-docker-hearth-1` from the fresh `.env.docker` value set.
- The Docker broker bridge now has the real URL and token:
  `HEARTH_BACKEND_BROKER_URL` / `HEARTH_BACKEND_BROKER_TOKEN` written to `.env.docker`.
- `hearth-backend-broker.service` and `lmstudio-server.service` are active on the host.

## Validation

- `systemctl --user is-active hearth-backend-broker.service lmstudio-server.service` -> `active`.
- `HEARTH_BACKEND_BROKER_URL` reachable on 127.0.0.1:8765.
- LM Studio API reported models on 127.0.0.1:1234.
- `hearth-postgres-1` healthy on 127.0.0.1:55432.
- `hearth-docker-postgres-1` healthy on 127.0.0.1:55434 plus `hearth-docker-hearth-1`
  healthy on `https://localhost:5444`.
