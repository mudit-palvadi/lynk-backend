"""A Redis or R2 outage must answer 503, not an unhandled 500 (#64).

Raw Redis calls (``zadd``, ``ttl``, ``zrange``, ``zrem``) and boto connection
failures (``EndpointConnectionError``) escape the services' own try/except
blocks, so the global handlers are the safety net.
"""

from unittest.mock import AsyncMock

import pytest
import redis.exceptions
from botocore.exceptions import EndpointConnectionError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.transfers import router as transfers_router
from app.core.exceptions import register_exception_handlers
from app.services.redis_service import redis_service

UNAVAILABLE = {"detail": "Redis or R2 storage unavailable"}


@pytest.fixture
def client():
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/redis")
    async def redis_outage():
        raise redis.exceptions.ConnectionError("redis is down")

    @app.get("/r2")
    async def r2_outage():
        raise EndpointConnectionError(endpoint_url="https://r2.example")

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_redis_error_becomes_503(client):
    response = client.get("/redis")
    assert response.status_code == 503
    assert response.json() == UNAVAILABLE


def test_r2_connection_error_becomes_503(client):
    response = client.get("/r2")
    assert response.status_code == 503
    assert response.json() == UNAVAILABLE


def test_unrelated_programming_error_is_not_swallowed():
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/bug")
    async def bug():
        raise ValueError("not a storage outage")

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/bug")
    assert response.status_code == 500


def test_real_route_maps_a_raw_redis_error_to_503(monkeypatch):
    """A real route over the real service, with Redis failing at the first call."""
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(transfers_router, prefix="/api/v1")

    monkeypatch.setattr(
        redis_service,
        "get_transfer",
        AsyncMock(side_effect=redis.exceptions.ConnectionError("redis is down")),
    )

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/api/v1/transfers/does-not-exist")

    assert response.status_code == 503
    assert response.json() == UNAVAILABLE
