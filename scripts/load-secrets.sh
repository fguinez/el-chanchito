#!/bin/sh
# Exports scraper secrets from the macOS login Keychain as env vars.
# Items are generic passwords named "chanchito.<VAR>" (see `make secrets-init`).
# Non-secret config (identifiers, hosts) stays in .env.
#
# Usage: . ./scripts/load-secrets.sh
# FINTUAL_TOKEN is the legacy name for FINTUAL_PASSWORD; both are loaded and
# the scraper prefers FINTUAL_PASSWORD.
for _key in BANCHILE_PASSWORD LIDER_BCI_PASSWORD FINTUAL_PASSWORD FINTUAL_TOKEN BUDA_API_KEY BUDA_API_SECRET EMAIL_IMAP_PASSWORD; do
  _val=$(security find-generic-password -a "$USER" -s "chanchito.$_key" -w 2>/dev/null)
  if [ -n "$_val" ]; then
    export "$_key=$_val"
  fi
done
unset _key _val
