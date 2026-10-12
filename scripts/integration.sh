#!/usr/bin/env bash
# Integration tests against a real platform.
#
# Without INSIGHTMON_IT_BASE_URL: boots a throwaway platform from INSIGHTMON_PLATFORM_DIR
# (default ../insightmon-ai-eval; its frontend must be built, `make build` there) with its E2E
# script: fresh SQLite database, demo data, loopback only. Then it creates an API key, runs
# tests/integration and stops the platform again (also on failure or Ctrl-C).
#
# With INSIGHTMON_IT_BASE_URL: uses that platform; creates a key unless INSIGHTMON_IT_API_KEY is
# set; never stops anything.
#
# A key this script created is revoked when it ends (also on failure or Ctrl-C), so no usable
# key is left behind on a shared platform.
#
#   INSIGHTMON_PLATFORM_DIR      platform checkout (default ../insightmon-ai-eval)
#   INSIGHTMON_IT_PORT           port of the booted platform (default 8790; its stub agent: +10)
#   INSIGHTMON_IT_DB             sqlite (default) or postgres (see the platform's E2E script)
#   INSIGHTMON_IT_ADMIN_EMAIL    admin used to create the key and read results back; for a
#   INSIGHTMON_IT_ADMIN_PASSWORD booted platform the demo admin of its seed script by default
#   INSIGHTMON_IT_BOOT_TIMEOUT   seconds to wait for the demo data (default 900)
#
# Extra arguments go to pytest, e.g. `scripts/integration.sh -k replay -x`.
# Secrets only live in this process's environment; nothing is written to the repository.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

say() { echo "integration: $*" >&2; }

PLATFORM_PGID=""
WORK_DIR=""
CREATED_KEY_ID=""
revoke_key() {
  [[ -n "$CREATED_KEY_ID" ]] || return 0
  say "revoking the key this run created"
  uv run python scripts/provision_platform.py --base-url "$INSIGHTMON_IT_BASE_URL" \
    --project-id "$INSIGHTMON_IT_PROJECT_ID" --revoke "$CREATED_KEY_ID" ||
    say "could not revoke key $CREATED_KEY_ID: revoke it on the project's API keys page"
  CREATED_KEY_ID=""
}
cleanup() {
  local status=$?
  trap - EXIT INT TERM
  revoke_key
  if [[ -n "$PLATFORM_PGID" ]]; then
    say "stopping the platform"
    kill -TERM -- "-$PLATFORM_PGID" 2>/dev/null || true
    for _ in $(seq 1 30); do
      kill -0 -- "-$PLATFORM_PGID" 2>/dev/null || break
      sleep 1
    done
    kill -KILL -- "-$PLATFORM_PGID" 2>/dev/null || true
  fi
  if [[ -n "$WORK_DIR" ]]; then
    if [[ $status -ne 0 && -f "$WORK_DIR/platform.log" ]]; then
      say "platform log (last 40 lines):"
      tail -40 "$WORK_DIR/platform.log" >&2 || true
    fi
    rm -rf "$WORK_DIR"
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

boot_platform() {
  local platform_dir port db timeout_s log
  platform_dir="$(cd "${INSIGHTMON_PLATFORM_DIR:-$ROOT/../insightmon-ai-eval}" && pwd)"
  port="${INSIGHTMON_IT_PORT:-8790}"
  db="${INSIGHTMON_IT_DB:-sqlite}"
  timeout_s="${INSIGHTMON_IT_BOOT_TIMEOUT:-900}"
  local script="$platform_dir/tests/e2e/scripts/start-backend.sh"
  [[ -x "$script" ]] || { say "no platform at $platform_dir (set INSIGHTMON_PLATFORM_DIR)"; exit 2; }
  [[ -f "$platform_dir/frontend/dist/index.html" ]] || {
    say "the platform's frontend is not built: run 'make build' in $platform_dir"
    exit 2
  }
  if curl -fsS "http://127.0.0.1:$port/healthz" >/dev/null 2>&1; then
    say "port $port is already in use (set INSIGHTMON_IT_PORT)"
    exit 2
  fi

  WORK_DIR="$(mktemp -d)"
  log="$WORK_DIR/platform.log"
  say "booting the platform from $platform_dir on port $port ($db); log $log"
  # Own process group (setsid), so that cleanup stops the server, the worker and the stub agent.
  TMPDIR="$WORK_DIR" E2E_DB="$db" E2E_PORT="$port" E2E_SEED=1 \
    setsid "$script" >"$log" 2>&1 < /dev/null &
  PLATFORM_PGID=$!

  local waited=0
  until grep -q "e2e: demo data ready" "$log" 2>/dev/null; do
    if ! kill -0 "$PLATFORM_PGID" 2>/dev/null; then
      say "the platform stopped while booting"
      exit 1
    fi
    if ((waited >= timeout_s)); then
      say "no demo data after ${timeout_s} s"
      exit 1
    fi
    sleep 2
    waited=$((waited + 2))
  done
  say "platform ready after ${waited} s"
  export INSIGHTMON_IT_BASE_URL="http://127.0.0.1:$port"

  if [[ -z "${INSIGHTMON_IT_ADMIN_EMAIL:-}" || -z "${INSIGHTMON_IT_ADMIN_PASSWORD:-}" ]]; then
    # The demo admin of the platform's own seed script (a throwaway local instance).
    local account
    account="$(cd "$platform_dir/backend" && uv run --frozen python -c \
      'import sys; sys.path.insert(0, "../scripts"); import seed_demo as s; print(s.ADMIN["email"]); print(s.ADMIN["password"])' \
      2>/dev/null)" || { say "cannot read the demo admin from the platform's seed script"; exit 2; }
    INSIGHTMON_IT_ADMIN_EMAIL="$(sed -n 1p <<<"$account")"
    INSIGHTMON_IT_ADMIN_PASSWORD="$(sed -n 2p <<<"$account")"
    export INSIGHTMON_IT_ADMIN_EMAIL INSIGHTMON_IT_ADMIN_PASSWORD
  fi
}

if [[ -z "${INSIGHTMON_IT_BASE_URL:-}" ]]; then
  boot_platform
fi
: "${INSIGHTMON_IT_ADMIN_EMAIL:?set INSIGHTMON_IT_ADMIN_EMAIL (an admin of the project)}"
: "${INSIGHTMON_IT_ADMIN_PASSWORD:?set INSIGHTMON_IT_ADMIN_PASSWORD}"

if [[ -z "${INSIGHTMON_IT_API_KEY:-}" ]]; then
  exports="$(uv run python scripts/provision_platform.py --base-url "$INSIGHTMON_IT_BASE_URL")"
  eval "$exports"
  unset exports
  CREATED_KEY_ID="$INSIGHTMON_IT_API_KEY_ID"
fi

say "running the integration tests against $INSIGHTMON_IT_BASE_URL"
uv run pytest -m integration tests/integration -p no:cacheprovider -ra "$@"
