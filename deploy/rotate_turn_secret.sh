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
CREATE_RESPONSE=$(curl -s -X POST "$API/api/v1/transfers" \
  -H 'Content-Type: application/json' \
  -d '{"files":[{"file_name":"rotation-check.txt","file_size":3,"content_type":"text/plain"}]}')

# #67: the create response is the only place the access token and sender secret
# appear. Keep them in variables - never echo them - and use them for the
# credential check (access token, #47) and the cleanup (sender secret).
TRANSFER_ID=$(printf '%s' "$CREATE_RESPONSE" \
  | sed -n 's/.*"transfer_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
ACCESS_TOKEN=$(printf '%s' "$CREATE_RESPONSE" \
  | sed -n 's/.*"access_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
SENDER_SECRET=$(printf '%s' "$CREATE_RESPONSE" \
  | sed -n 's/.*"sender_secret"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
CREATE_RESPONSE=""

PROBE_CANCELLED=0

# The probe transfer reserves capacity, so it must be cancelled on every exit
# path after creation. This runs only once, is guarded on both values, and
# re-exits with the status the script already had: a failed cancel must not
# turn a failed verification into a success (or vice versa).
cleanup() {
  local status=$?
  trap - EXIT
  if [ "$PROBE_CANCELLED" -eq 0 ] && [ -n "$TRANSFER_ID" ] && [ -n "$SENDER_SECRET" ]; then
    echo "==> Cancelling the probe transfer $TRANSFER_ID"
    cancel_probe || true
  fi
  exit "$status"
}

# DELETE answers 204 for both a real cancel and an unknown id, so a 204 here
# proves the request was accepted; anything else is a transport/API failure.
cancel_probe() {
  local code
  code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE \
    "$API/api/v1/transfers/$TRANSFER_ID" \
    -H "X-Lynk-Sender-Secret: $SENDER_SECRET" || true)
  PROBE_CANCELLED=1
  if [ "$code" = "204" ]; then
    echo "    probe transfer cancelled"
    return 0
  fi
  echo "!! Could not cancel the probe transfer (HTTP ${code:-no response})."
  echo "!! It holds capacity until its upload URLs expire; cancel it by hand."
  return 1
}

trap cleanup EXIT

if [ -z "$TRANSFER_ID" ] || [ -z "$ACCESS_TOKEN" ] || [ -z "$SENDER_SECRET" ]; then
  echo "!! The API did not return the tokens needed to verify the rotation."
  echo "!! The secret IS rotated, but verification could not proceed;"
  if [ -z "$TRANSFER_ID" ]; then
    echo "!! the probe transfer could not be identified, so it may hold capacity"
    echo "!! until it expires. Check the API logs and coturn config by hand."
  elif [ -z "$SENDER_SECRET" ]; then
    echo "!! the probe transfer $TRANSFER_ID cannot be cancelled without its"
    echo "!! sender secret. Cancel it by hand or wait for it to expire."
  else
    echo "!! the probe transfer $TRANSFER_ID will still be cancelled on exit."
    echo "!! Check the API logs and coturn config by hand."
  fi
  exit 1
fi

echo "==> Fetching TURN credentials for $TRANSFER_ID"
CREDS=$(curl -s "$API/api/v1/transfers/$TRANSFER_ID/turn-credentials" \
  -H "X-Lynk-Access-Token: $ACCESS_TOKEN")
USERNAME=$(printf '%s' "$CREDS" | sed -n 's/.*"username"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
CREDENTIAL=$(printf '%s' "$CREDS" | sed -n 's/.*"credential"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')

if [ -z "$USERNAME" ] || [ -z "$CREDENTIAL" ]; then
  echo "!! No credentials returned (the access token may have been rejected):"
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

echo "==> Cancelling the probe transfer $TRANSFER_ID"
if ! cancel_probe; then
  exit 1
fi

UNKNOWN_STATUS=$(curl -s -o /dev/null -w '%{http_code}' \
  "$API/api/v1/transfers/rotation-check-unknown/turn-credentials")
echo "==> Unknown id now answers $UNKNOWN_STATUS (404 = credentials are only minted for real transfers)"

echo "Rotation complete."
