#!/bin/sh
# Refuses to serve the dev dashboard beyond loopback without a password:
# `next dev` skips the login when DASHBOARD_PASSWORD is unset, so a LAN-facing
# DASHBOARD_HOST would hand every page and /api route to the whole network.
#
# Usage: . ./scripts/require-dashboard-password.sh   (exits the caller on refusal)
# `next dev` runs in apps/web and never reads the root .env, and
# scripts/load-secrets.sh deliberately skips DASHBOARD_PASSWORD, so the
# password has to come from the shell (see USAGE.md).
# Exact loopback names only: "127.0.0.1.example.com" resolves anywhere.
case "${DASHBOARD_HOST:-127.0.0.1}" in
  127.0.0.1|localhost|::1|"[::1]") ;;
  *)
    # Whitespace-only counts as unset, as it does in the app.
    if [ -z "$(printf '%s' "${DASHBOARD_PASSWORD:-}" | tr -d '[:space:]')" ]; then
      echo "!!  DASHBOARD_HOST=$DASHBOARD_HOST serves the dashboard beyond this machine," >&2
      echo "!!  but DASHBOARD_PASSWORD is empty and next dev would skip the login." >&2
      echo "!!  Export it in the same command, e.g.:" >&2
      echo "!!    DASHBOARD_PASSWORD='choose-a-long-random-one' DASHBOARD_HOST=$DASHBOARD_HOST make dev" >&2
      echo "!!  (see USAGE.md > Reaching the dashboard from another device)" >&2
      exit 1
    fi
    ;;
esac
