#!/bin/bash
# ---------------------------------------------------------------------------
# Installs the Lynk DOCKER-USER hardening on this host.
#
#   sudo bash deploy/install_docker_user_chain.sh
#
# Run it from a checkout of this repository on the backend VM. It copies the
# chain installer and the systemd units into place, enables them and runs the
# installer once with --verify (which rolls back automatically if the
# Cloudflare path is not serving 200).
#
# Rollback:
#   sudo iptables -F DOCKER-USER && sudo ip6tables -F DOCKER-USER
#   sudo systemctl disable --now lynk-docker-user.service lynk-docker-user.timer
# ---------------------------------------------------------------------------
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install -m 0755 "$HERE/docker_user_chain.sh" /usr/local/sbin/lynk-docker-user.sh
install -m 0644 "$HERE/lynk-docker-user.service" /etc/systemd/system/lynk-docker-user.service
install -m 0644 "$HERE/lynk-docker-user.timer" /etc/systemd/system/lynk-docker-user.timer

systemctl daemon-reload
systemctl enable --now lynk-docker-user.timer
systemctl enable lynk-docker-user.service

/usr/local/sbin/lynk-docker-user.sh --verify

echo
echo "=== unit status ==="
systemctl --no-pager status lynk-docker-user.service | head -12 || true
systemctl --no-pager list-timers lynk-docker-user.timer || true
