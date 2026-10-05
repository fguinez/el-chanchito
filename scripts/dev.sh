#!/bin/sh
# Full local dev stack in one command: postgres (Docker) + scraper service
# (host, with the on-demand control endpoint) + Next.js dashboard.
#
# Usage: scripts/dev.sh   (or `make dev`)
# Ctrl+C stops the dashboard and shuts the scraper service down with it.
# Scraper logs go to local/scrapers-dev.log (gitignored).

set -eu

cd "$(dirname "$0")/.."

# Next.js needs Node >= 20 (see package.json engines / .nvmrc). If the active
# node is older, try to switch via nvm before anything runs pnpm.
ensure_node() {
  node_major="$(node -v 2>/dev/null | sed -n 's/^v\([0-9]*\).*/\1/p')"
  if [ -n "$node_major" ] && [ "$node_major" -ge 20 ]; then
    return
  fi
  NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  if [ -s "$NVM_DIR/nvm.sh" ]; then
    . "$NVM_DIR/nvm.sh" >/dev/null 2>&1 || true
    nvm use >/dev/null 2>&1 || nvm use 20 >/dev/null 2>&1 || true
  fi
  node_major="$(node -v 2>/dev/null | sed -n 's/^v\([0-9]*\).*/\1/p')"
  if [ -z "$node_major" ] || [ "$node_major" -lt 20 ]; then
    echo "!!  Node >= 20 required (found: $(node -v 2>/dev/null || echo none))." >&2
    echo "!!  Install it (e.g. 'nvm install 20') and retry." >&2
    exit 1
  fi
}
ensure_node

# Secrets come from the macOS Keychain; identifiers come from .env (the
# scraper service loads it via python-dotenv).
. ./scripts/load-secrets.sh

DATABASE_URL="${DATABASE_URL:-postgres://finance:finance@localhost:5435/finance}"
export DATABASE_URL

# The control server binds 0.0.0.0, so probe the same address. 8080 is a
# popular port: if another stack already holds it, fall back to the next free
# one instead of crashing the scraper service (and pointing the dashboard at
# whatever else is listening there). An explicitly set port is never moved.
port_free() {
  .venv/bin/python -c 'import socket,sys
s = socket.socket()
try:
    s.bind(("0.0.0.0", int(sys.argv[1])))
except OSError:
    sys.exit(1)' "$1"
}
if [ -n "${SCRAPER_CONTROL_PORT:-}" ]; then
  if ! port_free "$SCRAPER_CONTROL_PORT"; then
    echo "!!  SCRAPER_CONTROL_PORT=$SCRAPER_CONTROL_PORT is already in use; pick another." >&2
    exit 1
  fi
else
  SCRAPER_CONTROL_PORT=8080
  while ! port_free "$SCRAPER_CONTROL_PORT"; do
    SCRAPER_CONTROL_PORT=$((SCRAPER_CONTROL_PORT + 1))
    if [ "$SCRAPER_CONTROL_PORT" -gt 8099 ]; then
      echo "!!  No free scraper control port in 8080-8099; set SCRAPER_CONTROL_PORT." >&2
      exit 1
    fi
  done
  if [ "$SCRAPER_CONTROL_PORT" -ne 8080 ]; then
    echo "==> Port 8080 is busy; scraper control endpoint will use :$SCRAPER_CONTROL_PORT"
  fi
fi

echo "==> Starting PostgreSQL"
docker compose up -d --wait postgres

echo "==> Running migrations"
pnpm --filter @chanchito/db-schema migrate

mkdir -p local
echo "==> Starting scraper service (control endpoint :$SCRAPER_CONTROL_PORT, log: local/scrapers-dev.log)"
(
  cd apps/scrapers
  SCRAPER_MODE=scheduled SCRAPER_CONTROL_PORT="$SCRAPER_CONTROL_PORT" \
    exec ../../.venv/bin/python main.py
) >>local/scrapers-dev.log 2>&1 &
SCRAPERS_PID=$!

cleanup() {
  kill "$SCRAPERS_PID" 2>/dev/null || true
}
trap 'exit 130' INT
trap 'exit 143' TERM
trap cleanup EXIT

# If the service died right away (e.g. no scrapers configured), say so instead
# of leaving the dashboard's refresh button to fail mysteriously.
sleep 1
if ! kill -0 "$SCRAPERS_PID" 2>/dev/null; then
  echo "!!  Scraper service exited early; see local/scrapers-dev.log."
  echo "!!  The dashboard still works, but on-demand refresh will be unavailable."
fi

echo "==> Starting dashboard on http://localhost:3000"
SCRAPER_CONTROL_URL="${SCRAPER_CONTROL_URL:-http://localhost:$SCRAPER_CONTROL_PORT}" \
  pnpm --filter @chanchito/web dev
