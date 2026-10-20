#!/usr/bin/env bash
# Rotate the Lynk TURN shared secret on BOTH sides.
#
# Usage:
#   On the TURN/API VM:   sudo bash deploy/rotate_turn_secret.sh
#
# It generates a new secret, patches /etc/turnserver.conf and the backend
# .env, restarts coturn, recreates the API container, then verifies that the
# API now mints credentials coturn will accept.
#
# Two details this must get right:
#
#   * `docker compose restart` does NOT re-read `env_file: .env` - the
#     container keeps the environment it was created with, so coturn would
#     hold the new secret while the API kept minting with the old one and
#     every TURN allocation would be rejected. Only `docker compose up -d`
#     recreates the container with the new secret.
#
#   * The check must use a real transfer id. The endpoint answers 404 for
#     unknown ids (POTENTIAL_ISSUES #25) and the minted credential has to be
#     re-derived from the new secret to prove the API really uses it.
set -euo pipefail

NEW_SECRET=$(head -c 32 /dev/urandom | base64)
CONF=/etc/turnserver.conf
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
COMPOSE_FILE="$ROOT_DIR/docker-compose.yml"
API="http://127.0.0.1:8000"

echo "==> Patching $CONF"
sudo sed -i "s|^static-auth-secret=.*|static-auth-secret=${NEW_SECRET}|" "$CONF"

echo "==> Patching $ENV_FILE"
if grep -q "^TURN_SECRET=" "$ENV_FILE"; then
  sudo sed -i "s|^TURN_SECRET=.*|TURN_SECRET=${NEW_SECRET}|" "$ENV_FILE"
else
  echo "TURN_SECRET=${NEW_SECRET}" | sudo tee -a "$ENV_FILE" >/dev/null
fi

echo "==> Restarting coturn"
sudo systemctl restart coturn

echo "==> Recreating the API container (up -d, not restart: env_file must be re-read)"
if command -v docker >/dev/null 2>&1 && [ -f "$COMPOSE_FILE" ]; then
  sudo docker compose -f "$COMPOSE_FILE" up -d
else
  echo "!! docker compose unavailable - restart the API manually so it re-reads .env"
fi

echo "==> Waiting for the API"
for _ in $(seq 1 30); do
  if curl -sf -o /dev/null "$API/"; then break; fi
  sleep 1
done

echo "==> Creating a transfer to exercise the endpoint"
TRANSFER_ID=$(curl -s -X POST "$API/api/v1/transfers" \
  -H 'Content-Type: application/json' \
  -d '{"files":[{"file_name":"rotation-check.txt","file_size":3,"content_type":"text/plain"}]}' \
  | sed -n 's/.*"transfer_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')

if [ -z "$TRANSFER_ID" ]; then
  echo "!! Could not create a transfer - cannot verify."
  echo "!! The secret IS rotated; check the API logs and coturn config by hand."
  exit 1
fi

echo "==> Fetching TURN credentials for $TRANSFER_ID"
CREDS=$(curl -s "$API/api/v1/transfers/$TRANSFER_ID/turn-credentials")
USERNAME=$(printf '%s' "$CREDS" | sed -n 's/.*"username"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
CREDENTIAL=$(printf '%s' "$CREDS" | sed -n 's/.*"credential"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')

if [ -z "$USERNAME" ] || [ -z "$CREDENTIAL" ]; then
  echo "!! No credentials returned:"
  printf '%s\n' "$CREDS" | head -c 300
  echo
  exit 1
fi

EXPECTED=$(printf '%s' "$USERNAME" \
  | openssl dgst -sha1 -hmac "$NEW_SECRET" -binary | base64)

if [ "$CREDENTIAL" = "$EXPECTED" ]; then
  echo "OK: the API mints credentials with the NEW secret, so coturn will accept them."
else
  echo "!! The API did not mint with the new secret - the container probably kept its old environment."
  echo "   expected $EXPECTED"
  echo "   got      $CREDENTIAL"
  exit 1
fi

UNKNOWN_STATUS=$(curl -s -o /dev/null -w '%{http_code}' \
  "$API/api/v1/transfers/rotation-check-unknown/turn-credentials")
echo "==> Unknown id now answers $UNKNOWN_STATUS (404 = credentials are only minted for real transfers)"

echo "Rotation complete."
