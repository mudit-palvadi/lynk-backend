#!/usr/bin/env bash
# Lynk TURN relay provisioning — coturn on a dedicated OCI Always-Free VM
# (Ubuntu 22.04/24.04). Run as root:  sudo bash coturn_setup.sh <SECRET>
#
# SECRET must match TURN_SECRET in the backend .env — the backend mints
# short-lived HMAC credentials with it; this box only verifies them.
set -euo pipefail

SECRET="${1:?Usage: coturn_setup.sh <TURN_SECRET>}"
LISTEN_PORT=3478
RELAY_MIN=49152
RELAY_MAX=65535

echo "==> Installing coturn"
apt-get update -qq
apt-get install -y coturn

echo "==> Writing /etc/turnserver.conf"
cat >/etc/turnserver.conf <<EOF
# Lynk P2P TURN relay
listening-port=${LISTEN_PORT}
listening-ip=0.0.0.0
external-ip=$(curl -s ifconfig.me)
fingerprint
lt-cred-mech
use-auth-secret
static-auth-secret=${SECRET}
realm=lynkshare.app
min-port=${RELAY_MIN}
max-port=${RELAY_MAX}
# Security hardening (POTENTIAL_ISSUES #18, #68)
no-multicast-peers
no-cli
# Coturn's denied-peer-ip takes an address or a start-end range; CIDR is
# rejected ("CIDR is not supported"), so every range is spelled out.
denied-peer-ip=0.0.0.0-0.255.255.255
denied-peer-ip=10.0.0.0-10.255.255.255
denied-peer-ip=100.64.0.0-100.127.255.255
denied-peer-ip=127.0.0.0-127.255.255.255
denied-peer-ip=169.254.0.0-169.254.255.255
denied-peer-ip=172.16.0.0-172.31.255.255
denied-peer-ip=192.168.0.0-192.168.255.255
# IPv6 loopback (::1/128), unique-local (fc00::/7), link-local (fe80::/10).
denied-peer-ip=::1-::1
denied-peer-ip=fc00::-fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff
denied-peer-ip=fe80::-febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff
# Quotas (POTENTIAL_ISSUES #68): one WebRTC session can open an allocation
# per stream, so 12 per user leaves room for several concurrent transfers
# while stopping one leaked credential from exhausting the relay. 1200 total
# allocations is comfortable for this VM's small footprint. 8 MiB/s
# (~67 Mbit/s) per session keeps large file transfers usable without letting
# a single relayed session saturate the link (coturn meters each direction
# separately).
user-quota=12
total-quota=1200
max-bps=8388608
EOF

echo "==> Enabling service"
systemctl enable --now coturn

echo "==> Firewall (ufw, if active)"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow ${LISTEN_PORT}/udp
  ufw allow ${LISTEN_PORT}/tcp
  ufw allow ${RELAY_MIN}:${RELAY_MAX}/udp
fi

cat <<'EOF'

=====================================================================
 VM-side setup done. NOW finish in the OCI Console (Security Lists):

   Ingress rules to add on this instance's subnet:
     3478/UDP   0.0.0.0/0   (TURN UDP)
     3478/TCP   0.0.0.0/0   (TURN TCP)
     49152-65535/UDP 0.0.0.0/0  (relay port range)

 Then set in backend .env and restart the API:
     TURN_URLS=turn:<THIS_VM_PUBLIC_IP>:3478
     TURN_SECRET=<the same secret you passed above>
     TURN_CREDENTIAL_TTL_SECONDS=600

 Verify relay candidates work:
     https://webrtc.github.io/samples/src/content/peerconnection/trickle-ice/
     -> add turn:<IP>:3478 with any username/cred minted by the endpoint
     -> look for entries with typ "relay"
=====================================================================
EOF