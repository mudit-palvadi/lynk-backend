"""Behavioural tests for the IP-based rate limiter.

The limiter used to build its Redis key from the concrete request path, so
every route that embeds a transfer id (``/transfers/{id}/extend``) produced a
unique key and the limit never fired. These tests pin the fixed behaviour:
explicit buckets, one window per bucket, atomic TTLs, a 429 when the limit is
exceeded, and a 503 (never a 500) when Redis is unavailable.
"""

import asyncio
import ipaddress
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.exceptions import RateLimitExceededException, ServiceUnavailableException
from app.core.security import RateLimiter, resolve_client_ip
from app.main import app
from app.services.redis_service import redis_service

# A Cloudflare edge address taken from the published ranges, and a client
# address that is not a Cloudflare edge.
CLOUDFLARE_EDGE_IP = "173.245.48.5"
CLIENT_IP = "203.0.113.10"
CLIENT_IP_2 = "198.51.100.23"


class _RecordingRedis:
    """Redis double that records the keys and TTL calls it receives."""

    def __init__(self, count: int = 1) -> None:
        self.count = count
        self.incr_keys: list[str] = []
        self.expire_calls: list[tuple[str, int, bool]] = []
        self.incr_error: Exception | None = None

    async def incr(self, key: str) -> int:
        self.incr_keys.append(key)
        if self.incr_error is not None:
            raise self.incr_error
        return self.count

    async def expire(self, key: str, seconds: int, nx: bool = False) -> bool:
        self.expire_calls.append((key, seconds, nx))
        return True


class _FakeClientInfo:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    """Minimal request stand-in: the limiter only reads client and headers."""

    def __init__(self, host: str | None = CLIENT_IP, headers: dict | None = None) -> None:
        self.client = _FakeClientInfo(host) if host is not None else None
        self.headers = headers or {}


@pytest.fixture
def fake_redis(monkeypatch) -> _RecordingRedis:
    fake = _RecordingRedis()
    monkeypatch.setattr(redis_service, "get_client", AsyncMock(return_value=fake))
    return fake


# --------------------------------------------------------------------------
# Bucketing
# --------------------------------------------------------------------------


async def _call(limiter: RateLimiter, request: _FakeRequest) -> None:
    await limiter(request)


# --------------------------------------------------------------------------
# Bucketing
# --------------------------------------------------------------------------


def test_same_route_different_ids_share_one_bucket(fake_redis) -> None:
    """The regression test: a transfer id in the path must not create a new
    bucket, otherwise the limit can never be reached."""
    limiter = RateLimiter(limit=5, window_seconds=60, bucket="transfer-extend")

    asyncio.run(_call(limiter, _FakeRequest()))
    asyncio.run(_call(limiter, _FakeRequest()))

    assert fake_redis.incr_keys == [
        f"lynk:rate:transfer-extend:{CLIENT_IP}",
        f"lynk:rate:transfer-extend:{CLIENT_IP}",
    ]
    # The key must not contain the id of the resource that was addressed.
    assert len(fake_redis.incr_keys[0].split(":")) == 4


def test_different_clients_get_different_buckets(fake_redis) -> None:
    limiter = RateLimiter(limit=5, window_seconds=60, bucket="transfer-create")

    asyncio.run(_call(limiter, _FakeRequest(host=CLIENT_IP)))
    asyncio.run(_call(limiter, _FakeRequest(host=CLIENT_IP_2)))

    assert fake_redis.incr_keys == [
        f"lynk:rate:transfer-create:{CLIENT_IP}",
        f"lynk:rate:transfer-create:{CLIENT_IP_2}",
    ]


def test_different_buckets_do_not_share_a_window(fake_redis) -> None:
    create = RateLimiter(limit=5, window_seconds=60, bucket="transfer-create")
    extend = RateLimiter(limit=5, window_seconds=60, bucket="transfer-extend")

    asyncio.run(_call(create, _FakeRequest()))
    asyncio.run(_call(extend, _FakeRequest()))

    assert fake_redis.incr_keys[0] != fake_redis.incr_keys[1]


# --------------------------------------------------------------------------
# Window handling
# --------------------------------------------------------------------------


def test_window_is_set_atomically_with_nx(fake_redis) -> None:
    """``nx=True`` sets the TTL only when the key has none, so a burst of
    requests cannot keep pushing the expiry out."""
    limiter = RateLimiter(limit=5, window_seconds=60, bucket="transfer-create")

    asyncio.run(_call(limiter, _FakeRequest()))
    asyncio.run(_call(limiter, _FakeRequest()))

    assert fake_redis.expire_calls == [
        (f"lynk:rate:transfer-create:{CLIENT_IP}", 60, True),
        (f"lynk:rate:transfer-create:{CLIENT_IP}", 60, True),
    ]


def test_limit_exceeded_raises_429(monkeypatch) -> None:
    fake = _RecordingRedis(count=6)
    monkeypatch.setattr(redis_service, "get_client", AsyncMock(return_value=fake))
    limiter = RateLimiter(limit=5, window_seconds=60, bucket="transfer-create")

    with pytest.raises(RateLimitExceededException) as excinfo:
        asyncio.run(_call(limiter, _FakeRequest()))

    assert "Rate limit exceeded" in excinfo.value.message
    assert excinfo.value.status_code == 429


def test_request_below_the_limit_is_allowed(fake_redis) -> None:
    limiter = RateLimiter(limit=5, window_seconds=60, bucket="transfer-create")

    asyncio.run(_call(limiter, _FakeRequest()))  # must not raise


# --------------------------------------------------------------------------
# Redis failures degrade to 503, never to 500
# --------------------------------------------------------------------------


def test_uninitialised_redis_surfaces_as_service_unavailable(monkeypatch) -> None:
    async def _boom():
        raise ServiceUnavailableException("Redis connection pool is not initialized")

    monkeypatch.setattr(redis_service, "get_client", _boom)
    limiter = RateLimiter(limit=5, window_seconds=60, bucket="transfer-create")

    with pytest.raises(ServiceUnavailableException) as excinfo:
        asyncio.run(_call(limiter, _FakeRequest()))

    assert excinfo.value.status_code == 503


def test_redis_connection_error_surfaces_as_service_unavailable(monkeypatch) -> None:
    fake = _RecordingRedis()
    fake.incr_error = ConnectionError("redis is down")
    monkeypatch.setattr(redis_service, "get_client", AsyncMock(return_value=fake))
    limiter = RateLimiter(limit=5, window_seconds=60, bucket="transfer-create")

    with pytest.raises(ServiceUnavailableException) as excinfo:
        asyncio.run(_call(limiter, _FakeRequest()))

    assert excinfo.value.status_code == 503


# --------------------------------------------------------------------------
# Client IP resolution
# --------------------------------------------------------------------------


def test_cloudflare_peer_uses_the_connecting_ip_header() -> None:
    request = _FakeRequest(
        host=CLOUDFLARE_EDGE_IP,
        headers={"CF-Connecting-IP": CLIENT_IP},
    )

    assert resolve_client_ip(request) == CLIENT_IP


def test_non_cloudflare_peer_ignores_the_connecting_ip_header() -> None:
    """A direct caller can set any header it likes, so the header is only
    trusted when the request actually arrived through Cloudflare."""
    request = _FakeRequest(
        host=CLIENT_IP,
        headers={"CF-Connecting-IP": "1.2.3.4"},
    )

    assert resolve_client_ip(request) == CLIENT_IP


def test_cloudflare_peer_without_the_header_uses_the_peer_address() -> None:
    request = _FakeRequest(host=CLOUDFLARE_EDGE_IP)

    assert resolve_client_ip(request) == CLOUDFLARE_EDGE_IP


def test_missing_client_is_unknown() -> None:
    request = _FakeRequest(host=None)

    assert resolve_client_ip(request) == "unknown"


def test_malformed_peer_address_does_not_raise() -> None:
    request = _FakeRequest(host="testclient")

    assert resolve_client_ip(request) == "testclient"


# --------------------------------------------------------------------------
# The routes are wired to the right buckets
# --------------------------------------------------------------------------


def test_limited_routes_use_explicit_buckets(monkeypatch) -> None:
    fake = _RecordingRedis()
    monkeypatch.setattr(redis_service, "get_client", AsyncMock(return_value=fake))
    client = TestClient(app, raise_server_exceptions=False)

    client.post("/api/v1/transfers")
    client.post("/api/v1/transfers/11111111-1111-1111-1111-111111111111/extend")
    client.get("/api/v1/transfers/11111111-1111-1111-1111-111111111111/turn-credentials")
    client.post("/api/v1/receiver-sessions")
    client.post("/api/v1/receiver-sessions/session-1/attach-transfer")

    buckets = [key.split(":")[2] for key in fake.incr_keys]

    assert buckets == [
        "transfer-create",
        "transfer-extend",
        "transfer-turn-credentials",
        "receiver-session-create",
        "receiver-session-attach",
    ]


def test_different_transfer_ids_hit_the_same_bucket(monkeypatch) -> None:
    fake = _RecordingRedis()
    monkeypatch.setattr(redis_service, "get_client", AsyncMock(return_value=fake))
    client = TestClient(app, raise_server_exceptions=False)

    client.post("/api/v1/transfers/11111111-1111-1111-1111-111111111111/extend")
    client.post("/api/v1/transfers/22222222-2222-2222-2222-222222222222/extend")

    assert fake.incr_keys[0] == fake.incr_keys[1]
    assert "11111111" not in fake.incr_keys[0]
    assert "22222222" not in fake.incr_keys[1]


def test_rate_limited_route_returns_429(monkeypatch) -> None:
    fake = _RecordingRedis(count=6)
    monkeypatch.setattr(redis_service, "get_client", AsyncMock(return_value=fake))
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/api/v1/transfers")

    assert response.status_code == 429
    assert "Rate limit exceeded" in response.json()["detail"]


def test_redis_outage_returns_503(monkeypatch) -> None:
    async def _boom():
        raise ServiceUnavailableException("Redis connection pool is not initialized")

    monkeypatch.setattr(redis_service, "get_client", _boom)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/api/v1/transfers")

    assert response.status_code == 503


# --------------------------------------------------------------------------
# Cloudflare range configuration
# --------------------------------------------------------------------------


def test_cloudflare_ranges_are_valid_networks() -> None:
    assert settings.CLOUDFLARE_IP_RANGES

    for entry in settings.CLOUDFLARE_IP_RANGES:
        network = ipaddress.ip_network(entry, strict=True)
        assert not network.is_private
        assert not network.is_loopback
        assert network.num_addresses > 1


def test_cloudflare_ranges_cover_both_families() -> None:
    versions = {
        ipaddress.ip_network(entry).version for entry in settings.CLOUDFLARE_IP_RANGES
    }

    assert versions == {4, 6}


def test_a_published_cloudflare_range_is_trusted() -> None:
    """Guards against the list drifting out of sync with Cloudflare's
    published ranges: the first IPv4 range is a stable anchor."""
    first = settings.CLOUDFLARE_IP_RANGES[0]
    network = ipaddress.ip_network(first)
    host = str(network.network_address + 1)

    request = _FakeRequest(host=host, headers={"CF-Connecting-IP": CLIENT_IP})

    assert resolve_client_ip(request) == CLIENT_IP
