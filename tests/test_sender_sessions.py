"""Sender sessions with a six-digit code (feature 1).

The sender owns the session and shows a code; the receiver types it and
publishes its public key. The code only names the session, so it must be
unique while it lives and must expire with the session it points at.
"""

import re

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.exceptions import (
    ServiceUnavailableException,
    TransferNotFoundException,
    TransferNotReadyException,
)
from app.main import app
from app.services.redis_service import redis_service
from app.services.sender_service import SenderService

pytestmark = pytest.mark.anyio

CODE_PATTERN = re.compile(r"^\d{6}$")
PK = "receiver-public-key-000000000000000000"
OTHER_PK = "another-public-key-111111111111111111"


@pytest.fixture()
async def fake_redis():
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    orig_get_client = redis_service.get_client

    async def _get_client():
        return fake

    redis_service.get_client = _get_client  # type: ignore[method-assign]
    redis_service.pool = object()  # truthy sentinel so get_client doesn't raise
    yield fake
    redis_service.get_client = orig_get_client  # type: ignore[method-assign]
    redis_service.pool = None
    await fake.flushall()
    await fake.aclose()


def _service() -> SenderService:
    return SenderService(redis_service=redis_service)


class TestCreateSenderSession:
    async def test_the_code_is_six_digits(self, fake_redis):
        created = await _service().create_sender_session()

        assert CODE_PATTERN.match(created["code"])
        assert created["status"] == "waiting"
        assert created["expires_in"] == settings.SENDER_SESSION_LIFETIME_SECONDS

    async def test_the_code_points_at_the_session_and_expires_with_it(
        self, fake_redis
    ):
        created = await _service().create_sender_session()

        assert await redis_service.get_sender_code(created["code"]) == created[
            "session_id"
        ]
        ttl = await fake_redis.ttl(f"lynk:sender_code:{created['code']}")
        assert 0 < ttl <= settings.SENDER_SESSION_LIFETIME_SECONDS

        stored = await redis_service.get_sender_session(created["session_id"])
        assert stored is not None
        assert stored["code"] == created["code"]
        assert stored["status"] == "waiting"
        assert stored["receiver_pk"] is None

    async def test_a_taken_code_is_skipped(self, fake_redis):
        service = _service()
        codes = iter(["123456", "123456", "654321"])
        service._generate_code = lambda: next(codes)  # type: ignore[method-assign]
        await redis_service.claim_sender_code("123456", "someone-else", 600)

        created = await service.create_sender_session()

        assert created["code"] == "654321"
        # The session that already held the code keeps it.
        assert await redis_service.get_sender_code("123456") == "someone-else"

    async def test_running_out_of_codes_fails_loudly(self, fake_redis):
        service = _service()
        service._generate_code = lambda: "000000"  # type: ignore[method-assign]
        await redis_service.claim_sender_code("000000", "someone-else", 600)

        with pytest.raises(ServiceUnavailableException):
            await service.create_sender_session()


class TestSenderSessionRoute:
    async def test_the_route_returns_the_code(self, fake_redis):
        with TestClient(app) as client:
            response = client.post("/api/v1/sender-sessions")

        assert response.status_code == 201
        body = response.json()
        assert CODE_PATTERN.match(body["code"])
        assert body["status"] == "waiting"
        assert body["expires_in"] == settings.SENDER_SESSION_LIFETIME_SECONDS


class TestJoinSenderSession:
    async def test_the_receiver_publishes_its_key(self, fake_redis):
        created = await _service().create_sender_session()

        joined = await _service().join_sender_session(created["code"], PK)

        assert joined["session_id"] == created["session_id"]
        assert joined["status"] == "joined"
        stored = await redis_service.get_sender_session(created["session_id"])
        assert stored["receiver_pk"] == PK
        assert stored["status"] == "joined"

    async def test_an_unknown_code_is_refused(self, fake_redis):
        with pytest.raises(TransferNotFoundException):
            await _service().join_sender_session("000000", PK)

    async def test_the_same_receiver_can_join_twice(self, fake_redis):
        created = await _service().create_sender_session()
        await _service().join_sender_session(created["code"], PK)

        again = await _service().join_sender_session(created["code"], PK)

        assert again["status"] == "joined"

    async def test_a_second_receiver_is_refused(self, fake_redis):
        created = await _service().create_sender_session()
        await _service().join_sender_session(created["code"], PK)

        with pytest.raises(TransferNotReadyException):
            await _service().join_sender_session(created["code"], OTHER_PK)

    async def test_an_expired_code_is_refused(self, fake_redis):
        created = await _service().create_sender_session()
        await fake_redis.delete(f"lynk:sender_code:{created['code']}")

        with pytest.raises(TransferNotFoundException):
            await _service().join_sender_session(created["code"], PK)


class TestSenderSessionPoll:
    async def test_the_sender_sees_the_key_but_never_a_wrapped_key(
        self, fake_redis
    ):
        created = await _service().create_sender_session()
        await _service().join_sender_session(created["code"], PK)

        view = await _service().get_sender_session(created["session_id"])

        assert view["status"] == "joined"
        assert view["receiver_pk"] == PK
        assert "wrapped_key" not in view
        assert 0 <= view["expires_in"] <= settings.SENDER_SESSION_LIFETIME_SECONDS

    async def test_an_unknown_session_is_refused(self, fake_redis):
        with pytest.raises(TransferNotFoundException):
            await _service().get_sender_session("nope")


class TestJoinRoute:
    async def test_the_route_joins_a_code(self, fake_redis):
        created = await _service().create_sender_session()

        with TestClient(app) as client:
            response = client.post(
                "/api/v1/sender-sessions/join",
                json={"code": created["code"], "receiver_pk": PK},
            )
            view = client.get(f"/api/v1/sender-sessions/{created['session_id']}")

        assert response.status_code == 200
        assert response.json()["status"] == "joined"
        assert view.status_code == 200
        assert view.json()["receiver_pk"] == PK
