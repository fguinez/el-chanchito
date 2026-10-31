#!/bin/sh
# Exports the scraper secrets from the secret store as env vars: the macOS
# Keychain, the Linux Secret Service (through the keyring package) or the
# owner-only local/secrets.env file on headless hosts. See
# scripts/secret_store.py and USAGE.md > Configuration.
# Non-secret config (identifiers, hosts) stays in .env.
#
# Usage: . ./scripts/load-secrets.sh   (from the repo root)
# A value already exported in the shell wins over the store. FINTUAL_TOKEN is
# the legacy name for FINTUAL_PASSWORD; both are loaded and the scraper prefers
# FINTUAL_PASSWORD. DASHBOARD_PASSWORD is deliberately not loaded (see
# scripts/require-dashboard-password.sh).
#
# Callers source this under `set -eu`, so nothing here may fail the caller: a
# missing interpreter or a store error only warns and loads nothing.
#
# Same interpreter choice as the Makefile's SECRET_STORE (.venv/bin/python,
# else python3), so `make secrets-init` and this script hit the same store.
if [ -x .venv/bin/python ]; then
  _chanchito_py=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
  _chanchito_py=python3
else
  _chanchito_py=
fi

if [ -z "$_chanchito_py" ]; then
  echo "!!  No Python found (.venv or python3); secrets not loaded. Run: make install-py" >&2
elif _chanchito_exports=$("$_chanchito_py" scripts/secret_store.py export); then
  eval "$_chanchito_exports"
else
  echo "!!  scripts/secret_store.py export failed; secrets not loaded." >&2
fi
unset _chanchito_py _chanchito_exports
