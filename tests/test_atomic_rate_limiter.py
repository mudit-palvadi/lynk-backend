"""The limiter must use one atomic Redis operation, not split INCR/EXPIRE."""

import asyncio
from unittest.mock import AsyncMock

import pytest
import fakeredis.aioredis
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from app.core.exceptions import (
    RateLimitExceededException,
    ServiceUnavailableException,
    register_exception_handlers,
)
from app.core.security import RateLimiter
from app.services.redis_service import redis_service


def request():
    return StarletteRequest({
        "type": "http", "method": "GET", "path": "/", "headers": [],
        "client": ("203.0.113.10", 1234),
    })


@pytest.fixture
def counter(monkeypatch):
    fake = AsyncMock()
    fake.eval.return_value = [1, 60000]
    fake.incr.return_value = 1
    fake.expire.return_value = True
    monkeypatch.setattr(redis_service, "get_client", AsyncMock(return_value=fake))
    return fake


def test_one_atomic_call_updates_count_and_expiry(counter):
    asyncio.run(RateLimiter(5, bucket="atomic-test")(request()))
    counter.eval.assert_awaited_once()
    args = counter.eval.await_args.args
    assert args[1:] == (1, "lynk:rate:atomic-test:203.0.113.10", 60000, 5)
    counter.incr.assert_not_awaited()
    counter.expire.assert_not_awaited()


def test_atomic_backend_failure_is_unavailable_not_allowed(counter):
    counter.eval.side_effect = RuntimeError("Redis unavailable")
    with pytest.raises(ServiceUnavailableException):
        asyncio.run(RateLimiter(5)(request()))


@pytest.mark.parametrize("response", [None, [], [1], ["bad", 60000], [1, -1]])
def test_invalid_atomic_response_fails_closed(counter, response):
    counter.eval.return_value = response
    with pytest.raises(ServiceUnavailableException):
        asyncio.run(RateLimiter(5)(request()))


def test_exceeded_window_reports_remaining_time_without_changing_json(counter):
    counter.eval.return_value = [6, 1250]
    app = FastAPI()
    register_exception_handlers(app)
    limiter = RateLimiter(5, bucket="atomic-test")

    @app.get("/limited")
    async def limited(req: Request):
        await limiter(req)
        return {"allowed": True}

    with TestClient(app) as client:
        response = client.get("/limited")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "2"
    assert response.json() == {
        "detail": "Rate limit exceeded. Maximum 5 requests per 60s."
    }


def test_concurrent_requests_share_a_bounded_expiring_counter(monkeypatch):
    async def exercise():
        store = fakeredis.aioredis.FakeRedis()
        monkeypatch.setattr(redis_service, "get_client", AsyncMock(return_value=store))
        try:
            limiter = RateLimiter(25, bucket="concurrent")
            results = await asyncio.gather(
                *(limiter(request()) for _ in range(500)), return_exceptions=True
            )
            assert results.count(None) == 25
            assert sum(isinstance(r, RateLimitExceededException) for r in results) == 475
            key = "lynk:rate:concurrent:203.0.113.10"
            assert int(await store.get(key)) == 26
            assert 0 < await store.pttl(key) <= 60000
        finally:
            await store.aclose()

    asyncio.run(exercise())


def test_requests_do_not_slide_an_existing_window(monkeypatch):
    async def exercise():
        store = fakeredis.aioredis.FakeRedis()
        monkeypatch.setattr(redis_service, "get_client", AsyncMock(return_value=store))
        try:
            limiter = RateLimiter(5, bucket="window")
            await limiter(request())
            key = "lynk:rate:window:203.0.113.10"
            await store.pexpire(key, 40000)
            await limiter(request())
            assert 0 < await store.pttl(key) <= 40000
            assert int(await store.get(key)) == 2
            await store.pexpire(key, 1)
            await asyncio.sleep(0.03)
            await limiter(request())
            assert int(await store.get(key)) == 1
            assert 40000 < await store.pttl(key) <= 60000
        finally:
            await store.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("existing_ttl", [None, 120000])
def test_missing_or_excessive_expiry_is_repaired_even_when_blocked(monkeypatch, existing_ttl):
    async def exercise():
        store = fakeredis.aioredis.FakeRedis()
        monkeypatch.setattr(redis_service, "get_client", AsyncMock(return_value=store))
        try:
            key = "lynk:rate:repair:203.0.113.10"
            await store.set(key, 6, px=existing_ttl)
            with pytest.raises(RateLimitExceededException) as error:
                await RateLimiter(5, bucket="repair")(request())
            assert error.value.headers == {"Retry-After": "60"}
            assert int(await store.get(key)) == 6
            assert 0 < await store.pttl(key) <= 60000
        finally:
            await store.aclose()

    asyncio.run(exercise())


def test_corrupt_counter_fails_closed(monkeypatch):
    async def exercise():
        store = fakeredis.aioredis.FakeRedis()
        monkeypatch.setattr(redis_service, "get_client", AsyncMock(return_value=store))
        try:
            await store.set("lynk:rate:corrupt:203.0.113.10", "invalid")
            with pytest.raises(ServiceUnavailableException):
                await RateLimiter(5, bucket="corrupt")(request())
        finally:
            await store.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("remaining", [0, 1, 1000, 1001, 60000])
def test_retry_after_is_rounded_up_and_never_zero(counter, remaining):
    counter.eval.return_value = [6, remaining]
    with pytest.raises(RateLimitExceededException) as error:
        asyncio.run(RateLimiter(5)(request()))
    assert int(error.value.headers["Retry-After"]) == max(1, (remaining + 999) // 1000)


@pytest.mark.parametrize("response", [[True, 1000], [0, 1000], [7, 1000], [1, 60001], [1, 1.5]])
def test_out_of_contract_values_fail_closed(counter, response):
    counter.eval.return_value = response
    with pytest.raises(ServiceUnavailableException):
        asyncio.run(RateLimiter(5)(request()))
