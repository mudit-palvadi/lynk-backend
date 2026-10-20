#!/bin/bash
# ---------------------------------------------------------------------------
# Lynk backend — DOCKER-USER hardening for the published API container port.
#
# Installed on the host as /usr/local/sbin/lynk-docker-user.sh and run from
# lynk-docker-user.service (boot) and lynk-docker-user.timer (hourly).
#
# Topology (verified on lynk-instance, 2026-10-20):
#   * The API runs in the `backend-web-1` container, published as
#     "8000:8000" => it is reachable on the HOST address, on every interface.
#   * Docker's nat table DNATs that port to the container
#     (`-A DOCKER ! -i br-29ac063c9008 -p tcp --dport 8000 -j DNAT
#      --to-destination 172.18.0.3:8000`), so traffic from outside arrives
#     on ens3 and is FORWARDed to the container.
#   * Cloudflare is the only legitimate client: it terminates TLS and
#     connects straight to the host's :8000. The container's own access log
#     shows the real Cloudflare edge IPs (104.22.176.2, 172.69.94.167), which
#     proves the DNAT/FORWARD path is the one in use.
#   * There is no host reverse proxy (nothing listens on 80/443) and the TURN
#     server listens on the host (3478 tcp/udp), i.e. in the INPUT chain,
#     which this work does not touch.
#
# Therefore DOCKER-USER is exactly the right hook: it is the first chain the
# FORWARD policy (DROP) consults, it survives Docker daemon restarts, and it
# lets us allow Cloudflare while dropping everything else that reaches the
# published API port — a second layer under the OCI security list.
#
# Rules installed, in order:
#   1. Cloudflare edge CIDRs -> RETURN      (the only allowed clients)
#   2. -i br-29ac063c9008  -> RETURN        (container egress / container<->container / hairpin)
#   3. -p tcp --dport 8000 -> DROP          (everything else to the API port)
#   4. -j RETURN                           (all other forwarded traffic untouched)
#
# Rollback:  iptables -F DOCKER-USER && ip6tables -F DOCKER-USER
#             systemctl disable --now lynk-docker-user.service
# ---------------------------------------------------------------------------
set -euo pipefail

API_PORT=8000
BRIDGE=br-29ac063c9008
CHAIN=DOCKER-USER
TMP_CHAIN=LYNK-TMP

# Cloudflare IP ranges (https://www.cloudflare.com/ips-v4 and /ips-v6).
CF_V4="173.245.48.0/20 103.21.244.0/22 103.22.200.0/22 103.31.4.0/22 141.101.64.0/18 108.162.192.0/18 190.93.240.0/20 188.114.96.0/20 197.234.240.0/22 198.41.128.0/17 162.158.0.0/15 104.16.0.0/13 104.24.0.0/14 172.64.0.0/13 131.0.72.0/22"
CF_V6="2400:cb00::/32 2606:4700::/32 2803:f800::/32 2405:b500::/32 2405:8100::/32 2a06:98c0::/29 2c0f:f248::/32"

# build_chain <iptables-binary> <chain> <cidr-list>
build_chain() {
  local bin="$1" chain="$2" list="$3" cidr
  "$bin" -N "$chain" 2>/dev/null || true
  "$bin" -F "$chain"
  for cidr in $list; do
    "$bin" -A "$chain" -p tcp -m tcp --dport "$API_PORT" -s "$cidr" -j RETURN
  done
  "$bin" -A "$chain" -i "$BRIDGE" -j RETURN
  "$bin" -A "$chain" -p tcp -m tcp --dport "$API_PORT" -j DROP
  "$bin" -A "$chain" -j RETURN
}

# canonical <iptables-binary> <chain> -> the chain's rules, normalised
canonical() {
  "$1" -S "$2" 2>/dev/null | sed "s|^-A $2 ||" | grep -v '^-N ' || true
}

# install_for <iptables-binary> <cidr-list>
install_for() {
  local bin="$1" list="$2"

  # Compare against a scratch chain first: iptables normalises rule syntax
  # (-m tcp is implied by --dport), so comparing the rendered form avoids
  # rebuilding the chain on every run and keeps the window where the chain is
  # empty down to zero.
  build_chain "$bin" "$TMP_CHAIN" "$list"
  local want have
  want="$(canonical "$bin" "$TMP_CHAIN")"
  "$bin" -X "$TMP_CHAIN" 2>/dev/null || true

  have="$(canonical "$bin" "$CHAIN")"
  if [ "$want" = "$have" ]; then
    echo "[$bin] $CHAIN already matches the expected rules - nothing to do."
    return 0
  fi

  build_chain "$bin" "$CHAIN" "$list"

  # Docker keeps DOCKER-USER across daemon restarts, but make sure the
  # FORWARD jump is still there and still comes first.
  if ! "$bin" -C FORWARD -j "$CHAIN" 2>/dev/null; then
    "$bin" -I FORWARD 1 -j "$CHAIN"
    echo "[$bin] re-added the FORWARD -> $CHAIN jump."
  fi

  echo "[$bin] $CHAIN installed."
  "$bin" -L "$CHAIN" -n -v --line-numbers
}

echo "=== installing DOCKER-USER rules (IPv4) ==="
install_for iptables "$CF_V4"

echo "=== installing DOCKER-USER rules (IPv6) ==="
install_for ip6tables "$CF_V6"

echo
echo "--- iptables -L DOCKER-USER ---"
iptables -L "$CHAIN" -n -v --line-numbers
echo "--- ip6tables -L DOCKER-USER ---"
ip6tables -L "$CHAIN" -n -v --line-numbers || true

# The verification curls are opt-in (--verify). They are NOT run by the
# systemd service/timer on purpose: if Cloudflare were briefly unreachable,
# an automatic rollback would flush the chain and re-open the API port to the
# whole internet. Rolling back is a deliberate, human decision.
if [ "${1:-}" = "--verify" ]; then
  echo
  echo "=== verification ==="
  echo "--- local API (must stay 200; it uses the loopback/docker-proxy path) ---"
  curl -s -o /dev/null -w 'local  http://127.0.0.1:8000/ -> %{http_code}\n' --max-time 10 http://127.0.0.1:8000/

  echo "--- public API through Cloudflare (must stay 200) ---"
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 https://api.lynkshare.app/ || echo 000)"
  echo "public https://api.lynkshare.app/ -> $code"
  if [ "$code" != "200" ]; then
    echo "!! Cloudflare path is broken - rolling back DOCKER-USER immediately." >&2
    iptables -F "$CHAIN" || true
    ip6tables -F "$CHAIN" || true
    echo "rolled back (DOCKER-USER flushed)." >&2
    exit 1
  fi
fi

echo
echo "DOCKER-USER hardening complete."
