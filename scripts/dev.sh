#!/bin/sh
# Full local dev stack in one command: postgres (Docker) + scraper service
# (host, with the on-demand control endpoint) + Next.js dashboard.
#
# Usage: scripts/dev.sh   (or `make dev`)
# Ctrl+C stops the dashboard and shuts the scraper service down with it.
# Scraper logs go to local/scrapers-dev.log (gitignored).

set -eu

cd "$(dirname "$0")/.."

# Secrets come from the macOS Keychain; identifiers come from .env (the
# scraper service loads it via python-dotenv).
. ./scripts/load-secrets.sh

DATABASE_URL="${DATABASE_URL:-postgres://finance:finance@localhost:5435/finance}"
SCRAPER_CONTROL_PORT="${SCRAPER_CONTROL_PORT:-8080}"
export DATABASE_URL

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
