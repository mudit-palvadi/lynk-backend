"""The TURN relay must not be usable to reach private or local networks.

POTENTIAL_ISSUES #68: ``deploy/coturn_setup.sh`` denied only the RFC 1918
ranges and the link-local block. A relay that is reachable from the internet
also has to refuse the "this network", loopback, carrier-grade NAT, and the
IPv6 loopback/unique-local/link-local ranges, or a TURN allocation becomes an
SSRF pivot into the VM's own network.

It also set no quotas, so a single credential could open unlimited
allocations and consume unlimited bandwidth.

These are provisioning contracts, so they are asserted against the script
rather than against a running relay: the script needs root and a real VM.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COTURN_SCRIPT = REPO_ROOT / "deploy" / "coturn_setup.sh"

# Required denied-peer-ip entries. Coturn's parser splits on "-" and accepts a
# single address or a start-end range; it rejects CIDR notation outright
# ("CIDR is not supported"), in 4.5.x and 4.6.x alike. The ranges below are
# therefore the expanded form of the CIDR blocks the issue lists.
REQUIRED_DENIED_RANGES = {
    "0.0.0.0/8 (this network)": "0.0.0.0-0.255.255.255",
    "10.0.0.0/8 (private)": "10.0.0.0-10.255.255.255",
    "100.64.0.0/10 (carrier-grade NAT)": "100.64.0.0-100.127.255.255",
    "127.0.0.0/8 (IPv4 loopback)": "127.0.0.0-127.255.255.255",
    "169.254.0.0/16 (IPv4 link-local)": "169.254.0.0-169.254.255.255",
    "172.16.0.0/12 (private)": "172.16.0.0-172.31.255.255",
    "192.168.0.0/16 (private)": "192.168.0.0-192.168.255.255",
    "::1/128 (IPv6 loopback)": "::1-::1",
    "fc00::/7 (IPv6 unique-local)": "fc00::-fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
    "fe80::/10 (IPv6 link-local)": "fe80::-febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
}


@pytest.fixture(scope="module")
def script_text() -> str:
    return COTURN_SCRIPT.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def denied_ranges(script_text: str) -> list[str]:
    """The values of every active ``denied-peer-ip=`` line."""
    return re.findall(r"^denied-peer-ip=(\S+)\s*$", script_text, flags=re.MULTILINE)


@pytest.fixture(scope="module")
def config_value(script_text: str):
    def _lookup(key: str) -> int | None:
        match = re.search(rf"^{re.escape(key)}=(\d+)\s*$", script_text, flags=re.MULTILINE)
        return int(match.group(1)) if match else None

    return _lookup


def test_the_script_exists() -> None:
    assert COTURN_SCRIPT.is_file()


@pytest.mark.parametrize(
    ("label", "expected"),
    REQUIRED_DENIED_RANGES.items(),
    ids=REQUIRED_DENIED_RANGES.keys(),
)
def test_every_private_range_is_denied(
    denied_ranges: list[str], label: str, expected: str
) -> None:
    assert expected in denied_ranges, f"{label} is not denied"


def test_the_deny_list_uses_ranges_not_cidr(denied_ranges: list[str]) -> None:
    """Coturn refuses a CIDR value and exits at startup.

    A config that aborts on boot is worse than a missing entry: the relay is
    down. This pins the spelling that all supported Ubuntu coturn packages
    (4.5.2 on 22.04, 4.6.2 on 24.04) actually parse.
    """
    assert denied_ranges, "the script defines no denied-peer-ip entries"
    offenders = [value for value in denied_ranges if "/" in value]
    assert not offenders, f"CIDR is not valid for denied-peer-ip: {offenders}"


def test_multicast_and_cli_stay_disabled(script_text: str) -> None:
    assert re.search(r"^no-multicast-peers\s*$", script_text, flags=re.MULTILINE)
    assert re.search(r"^no-cli\s*$", script_text, flags=re.MULTILINE)


def test_the_listener_and_relay_ports_are_unchanged(script_text: str) -> None:
    assert "listening-port=${LISTEN_PORT}" in script_text
    assert "min-port=${RELAY_MIN}" in script_text
    assert "max-port=${RELAY_MAX}" in script_text
    assert "LISTEN_PORT=3478" in script_text
    assert "RELAY_MIN=49152" in script_text
    assert "RELAY_MAX=65535" in script_text


def test_the_realm_and_secret_behavior_are_unchanged(script_text: str) -> None:
    assert "realm=lynkshare.app" in script_text
    assert "use-auth-secret" in script_text
    assert "static-auth-secret=${SECRET}" in script_text


def test_a_per_user_allocation_quota_is_set(config_value) -> None:
    assert config_value("user-quota") == 12


def test_a_total_allocation_quota_is_set(config_value) -> None:
    assert config_value("total-quota") == 1200


def test_a_usable_bandwidth_cap_is_set(config_value) -> None:
    max_bps = config_value("max-bps")
    assert max_bps == 8388608
    # Guard against a future "quota" that would make the relay useless for a
    # file transfer: the cap must stay in the tens of Mbit/s at least.
    assert max_bps >= 1_000_000, "max-bps is too low for a usable file transfer"


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bash") is None,
    reason="bash syntax check runs on the Ubuntu CI runner",
)
def test_the_script_parses_with_bash() -> None:
    """A syntax error would only surface on the VM, as a half-run setup.

    The backend CI runs on Ubuntu; on a Windows checkout this skips rather
    than failing (a ``bash`` on PATH there may be a WSL relay with no distro,
    which cannot parse a Windows path).
    """
    result = subprocess.run(
        ["bash", "-n", str(COTURN_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
