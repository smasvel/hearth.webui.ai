# Docker install verification with Tailscale — 2026-10-30

Tested on an existing Linux host (Ubuntu, Docker Engine 29.1.3, Compose 2.40.3,
Tailscale 1.104.4). This created a **new, separate** Compose project next to the
native host installation and development PostgreSQL container. Neither existing stack was
read, migrated, or modified.

## Fresh install

```bash
bash deploy/install-docker.sh --tailscale-https-port 10443
```

- Created `.env.docker` with a random database password, `POSTGRES_PORT=55434`,
  `HEARTH_PORT=5444`, and `HEARTH_DOCKER_STATE_DIR=data/docker-hearth`
  (ports were free at install time).
- Built `hearth-docker-hearth:latest` from the working tree and started
  `hearth-docker-postgres-1` + `hearth-docker-hearth-1`.
- Both containers reported `healthy`; agent runtime bind mount is writable and the
  container can reach the host Docker socket.
- `https://localhost:5444/` redirects to `/login` and shows **Create administrator**,
  so first-visit account creation is the remaining manual step.
- The new tailnet URL's `/login` returned `200` over the tailnet with
  a Tailscale-issued certificate (no browser warning, no `-k` needed).
- `tailscale serve status` shows the new route and keeps the two pre-existing routes:

  ```
  https://<tailnet-device>:10000  -> https+insecure://<host-lan-address>:5000
  https://<tailnet-device>:10443  -> https+insecure://127.0.0.1:5444   (new)
  https://<tailnet-device>:8443   -> https+insecure://127.0.0.1:5443
  ```

## Gap found and fixed

The container had no `tailscale` binary, so the app's own Help panel and startup log
could not detect the tailnet: `tailscale ip`, `tailscale status`, and
`tailscale serve status` all failed inside the container even though Serve itself
worked. That made the Docker instance unable to show its tailnet address or the
connect configs built from it.

`deploy/install-docker.sh` now detects a running host daemon and adds
`compose.tailscale.yaml`, which mounts the host CLI read-only at
`/usr/local/bin/tailscale` and the daemon socket at `/run/tailscale`. Hosts without
Tailscale are unaffected because the override file is not used.

Verified after recreating the container:

```
$ docker exec hearth-docker-hearth-1 sh -c 'tailscale ip -4; python -c "...help_data..."'
<tailnet-ip>
<tailnet-device>
https://<tailnet-device>:10443
```

and the startup log prints the configured Tailscale access URL.

Also hardened: `--tailscale-https-port` now refuses to run when Tailscale is installed
but its daemon is not connected, instead of failing later inside `tailscale serve`.

## Rerun behavior

Rerunning the installer rebuilt the image, recreated only the app container, kept the
PostgreSQL container and all volumes, and left the Serve route unchanged. Rerunning is
also required after installing or reconnecting Tailscale so the container gets the
CLI/socket mounts.

## Model gateway keys

The host's existing hand-installed Caddy block was detected and left byte-for-byte
unchanged (legacy marker). The installer mounted `/etc/caddy/Caddyfile` read-only via
`compose.caddy.yaml`, and inside the container:

- `help_data.parse_caddyfile()` read all six tokens and all five backend ports;
- `help_data.ca_certificate()` fetched Caddy's local CA root (630 bytes);
- `GET https://localhost:9443/v1/models` returned `200` with the `lan` key and `401`
  without it.

Because the host already had a gateway with a legacy block, none of its tokens were
rotated. New hosts get generated tokens and a managed block from
`deploy/install-host-deps.sh`; the Docker installer publishes that block through
passwordless sudo when available and otherwise prints the exact `sudo` command.

## Notes

- Local HTTPS is still self-signed; the Tailscale URL is the trusted-certificate path.
- Serve is tailnet-only; no Funnel/public exposure was enabled.
- The Docker instance keeps its own accounts, database volume, and certificates.
- Stop with `docker compose --env-file .env.docker -f compose.docker.yaml stop`;
  `docker compose ... down --volumes` would delete its data.
