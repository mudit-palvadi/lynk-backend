"""#51: a request body is bounded before it is parsed or stored.

The receiver-session attach took a wrapped key of any length with no rate
limit, and Redis had no memory cap, so about 100 MB per request could push the
VM out of memory. Parsing such a body also blocked the single worker.
"""

from pathlib import Path
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import settings
from app.main import app
from app.schemas.receiver import AttachTransferRequest
from app.services.redis_service import redis_service


@pytest.fixture()
def client(monkeypatch):
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_service, "get_client", AsyncMock(return_value=fake))
    monkeypatch.setattr(redis_service, "get_receiver_session", AsyncMock(return_value=None))
    return TestClient(app, raise_server_exceptions=False)


def test_an_oversized_body_is_refused_with_413(client) -> None:
    body = "x" * (settings.MAX_REQUEST_BODY_BYTES + 1)

    resp = client.post(
        "/api/v1/receiver-sessions/session-1/attach-transfer",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 413
    assert resp.json() == {"detail": "Request body is too large"}


def test_an_oversized_body_without_a_length_header_is_refused(client) -> None:
    def chunks():
        for _ in range(4):
            yield b"x" * (settings.MAX_REQUEST_BODY_BYTES // 2)

    resp = client.post(
        "/api/v1/transfers",
        content=chunks(),
        headers={"Content-Type": "application/json"},
    )

    assert resp.status_code == 413


def test_a_normal_body_still_reaches_the_route(client) -> None:
    resp = client.post(
        "/api/v1/receiver-sessions/session-1/attach-transfer",
        json={"transfer_id": "t-1", "wrapped_key": "k" * 200},
    )

    # The fake Redis has no such session, so the route answers 404, which
    # proves the body was replayed and parsed.
    assert resp.status_code == 404


def test_the_attach_fields_are_bounded() -> None:
    with pytest.raises(ValidationError):
        AttachTransferRequest(transfer_id="t-1", wrapped_key="k" * 1001)
    with pytest.raises(ValidationError):
        AttachTransferRequest(transfer_id="t" * 65)
    assert AttachTransferRequest(transfer_id="t-1").wrapped_key is None


def test_compose_caps_redis_memory() -> None:
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()

    assert "--maxmemory 256mb" in compose
    assert "--maxmemory-policy noeviction" in compose
