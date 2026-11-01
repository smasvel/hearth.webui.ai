#!/usr/bin/env bash
# Idempotently bring up LM Studio's headless daemon and API server.
#
# Hearth needs LM Studio's API (localhost:1234) for inventory, load/unload and the
# app's shared `lms` CLI, but the daemon and API server are two separate LM Studio
# processes: `lms daemon up` can be running while the API server is stopped (confirmed
# live), which looks like an LM Studio outage. This helper is safe to run at boot or
# any time — it restarts the API server if it is already up, otherwise starts it.
set -euo pipefail

port=${LMSTUDIO_PORT:-1234}
probe_url=${LMSTUDIO_PROBE_URL:-http://127.0.0.1:${port}/api/v0/models}

if [[ -z ${arg:-} && $# -gt 0 ]]; then
  echo "Unknown option: $1" >&2
  exit 1
fi

lms=$(command -v lms || true)
if [[ -z $lms ]]; then
  for candidate in "$HOME/.lmstudio/bin/lms" "$HOME/.lmstudio/llmster/0.0.25-1/.bundle/bin/lms"; do
    if [[ -x $candidate ]]; then
      lms=$candidate
      break
    fi
  done
fi
if [[ -z $lms ]]; then
  # Hearth treats the absence of LM Studio as a normal (not installed) backend. Keep
  # the same posture here: fail soft so the Docker bridge and other backends survive.
  echo 'LM Studio CLI (lms) not found; skipping the API bootstrap.' >&2
  exit 0
fi

probe() {
  if [[ -z ${LMSTUDIO_PROBE_METHOD:-} ]]; then
    if command -v curl >/dev/null; then
      curl --silent --max-time 4 --output /dev/null --fail "$probe_url" || return 1
      return 0
    fi
  fi
  "$lms" server status | grep -q 'not running' && return 1
  return 0
}

# 1. Headless daemon — a separate process from the API server.
case "$("$lms" daemon status 2>&1 || true)" in
  *running*) ;;
  *)
    echo 'Starting LM Studio headless daemon...'
    "$lms" daemon up
    ;;
esac

# 2. OpenAI-compatible API server — the endpoint Hearth talks to.
if probe; then
  echo "LM Studio API already running on port $port."
  exit 0
fi

# `lms server start` is idempotent upstream but can emit "already running" warnings;
# run it only when this runtime doesn't already serve the same endpoint.
previous="$(timeout 8 "$lms" server status 2>&1 || true)"
case "$previous" in
  *running*)
    # The lms CLI thinks it is running, but the API probe disagrees: restart it so
    # Hearth gets a working endpoint instead of a stale internal state.
    "$lms" server stop || true
    sleep 1
    ;;
esac
echo "Starting LM Studio API server on port $port..."
"$lms" server start --port "$port"

# Confirm before reporting success: a start that "succeeded" upstream but never opened
# the port would otherwise cause Hearth to fail 5s later on the next request.
for _ in $(seq 1 10); do
  if probe; then
    echo "LM Studio API is now running on port $port."
    exit 0
  fi
  sleep 1
done
echo "LM Studio API did not accept HTTP requests on $probe_url" >&2
exit 1
