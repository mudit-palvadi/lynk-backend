"""Canonical client identities and additional IPv6 network budgets."""

import asyncio
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from app.core.config import Settings, settings
from app.core.exceptions import RateLimitExceededException
from app.core.security import RateLimiter, resolve_client_address
from app.services.redis_service import redis_service


@pytest.mark.parametrize(
    ("peer", "canonical"),
    [
        ("2001:0DB8:0000:0000:0000:0000:0000:0001", "2001:db8::1"),
        ("2001:db8::1", "2001:db8::1"),
        ("::ffff:203.0.113.10", "203.0.113.10"),
        ("::FFFF:CB00:710A", "203.0.113.10"),
        ("203.0.113.10", "203.0.113.10"),
        ("fe80::1%eth0", "fe80::1"),
    ],
)
def test_peer_addresses_have_one_canonical_identity(peer, canonical):
    assert resolve_client_address(peer, None) == canonical


def test_trusted_forwarded_addresses_are_canonicalized(monkeypatch):
    monkeypatch.setattr(settings, "CLOUDFLARE_EDGE_SECRET", "edge-test")
    assert resolve_client_address(
        "173.245.48.5", "2001:0DB8:0:0:0:0:0:1", "edge-test"
    ) == "2001:db8::1"
    assert resolve_client_address(
        "::ffff:173.245.48.5", "::ffff:203.0.113.10", "edge-test"
    ) == "203.0.113.10"


def test_forwarding_never_bypasses_peer_and_secret_checks(monkeypatch):
    monkeypatch.setattr(settings, "CLOUDFLARE_EDGE_SECRET", "edge-test")
    assert resolve_client_address(
        "2001:0DB8:0:0:0:0:0:1", "198.51.100.2", "edge-test"
    ) == "2001:db8::1"
    assert resolve_client_address(
        "173.245.48.5", "198.51.100.2", "wrong"
    ) == "173.245.48.5"


def test_forwarded_scope_identifiers_are_not_client_identities(monkeypatch):
    monkeypatch.setattr(settings, "CLOUDFLARE_EDGE_SECRET", "edge-test")
    assert resolve_client_address(
        "173.245.48.5", "2001:db8::1%rotating-zone", "edge-test"
    ) == "173.245.48.5"


class _Counters:
    def __init__(self):
        self.counts = Counter()
        self.expiries = []

    async def incr(self, key):
        self.counts[key] += 1
        return self.counts[key]

    async def expire(self, key, seconds, nx=False):
        self.expiries.append((key, seconds, nx))
        return True


def _request(address):
    return Request({"type": "http", "client": (address, 1234), "headers": []})


@pytest.fixture
def counters(monkeypatch):
    store = _Counters()
    monkeypatch.setattr(redis_service, "get_client", AsyncMock(return_value=store))
    return store


def test_equivalent_ipv4_mapped_addresses_share_the_client_counter(counters):
    limiter = RateLimiter(limit=2, bucket="identity-test")

    async def exercise():
        await limiter(_request("203.0.113.10"))
        await limiter(_request("::ffff:203.0.113.10"))
        with pytest.raises(RateLimitExceededException):
            await limiter(_request("::ffff:cb00:710a"))

    asyncio.run(exercise())
    assert list(counters.counts) == ["lynk:rate:identity-test:203.0.113.10"]


def test_rotated_ipv6_addresses_share_an_additional_prefix_budget(counters, monkeypatch):
    monkeypatch.setattr("app.core.security.settings", SimpleNamespace(
        RATE_LIMIT_IPV6_PREFIX_LENGTH=64,
        CLOUDFLARE_EDGE_SECRET="",
        CLOUDFLARE_IP_RANGES=settings.CLOUDFLARE_IP_RANGES,
    ))
    limiter = RateLimiter(limit=2, bucket="identity-test", scope="ipv6-prefix")

    async def exercise():
        await limiter(_request("2001:db8:1234:5678::1"))
        await limiter(_request("2001:DB8:1234:5678:aaaa:bbbb:cccc:dddd"))
        with pytest.raises(RateLimitExceededException):
            await limiter(_request("2001:db8:1234:5678::9999"))
        await limiter(_request("2001:db8:1234:5679::1"))

    asyncio.run(exercise())
    assert counters.counts == {
        "lynk:rate:identity-test:ipv6:2001:db8:1234:5678::/64": 3,
        "lynk:rate:identity-test:ipv6:2001:db8:1234:5679::/64": 1,
    }
    assert all(seconds == 60 and nx for _, seconds, nx in counters.expiries)


def test_prefix_length_is_configurable(counters, monkeypatch):
    monkeypatch.setattr("app.core.security.settings", SimpleNamespace(
        RATE_LIMIT_IPV6_PREFIX_LENGTH=48,
        CLOUDFLARE_EDGE_SECRET="",
        CLOUDFLARE_IP_RANGES=settings.CLOUDFLARE_IP_RANGES,
    ))
    limiter = RateLimiter(limit=1, bucket="identity-test", scope="ipv6-prefix")

    async def exercise():
        await limiter(_request("2001:db8:1234:1::1"))
        with pytest.raises(RateLimitExceededException):
            await limiter(_request("2001:db8:1234:ffff::2"))

    asyncio.run(exercise())
    assert list(counters.counts) == [
        "lynk:rate:identity-test:ipv6:2001:db8:1234::/48"
    ]


def test_ipv4_has_no_extra_ipv6_prefix_charge(counters):
    limiter = RateLimiter(limit=1, bucket="identity-test", scope="ipv6-prefix")

    async def exercise():
        await limiter(_request("203.0.113.10"))
        await limiter(_request("::ffff:203.0.113.10"))

    asyncio.run(exercise())
    assert not counters.counts


@pytest.mark.parametrize("length", [0, 129, -1])
def test_invalid_ipv6_prefix_configuration_is_rejected(length):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, RATE_LIMIT_IPV6_PREFIX_LENGTH=length)


def test_default_ipv6_prefix_is_64_bits():
    assert Settings(_env_file=None).RATE_LIMIT_IPV6_PREFIX_LENGTH == 64
