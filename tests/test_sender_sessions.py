"""Sender sessions with a six-digit code (feature 1).

The sender owns the session and shows a code; the receiver types it and
publishes its public key. The code only names the session, so it must be
unique while it lives and must expire with the session it points at.
"""

import re
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.exceptions import (
    ServiceUnavailableException,
    TransferNotFoundException,
    TransferNotReadyException,
    UnauthorizedException,
)
from app.main import app
from app.services.redis_service import redis_service
from app.services.sender_service import SenderService

pytestmark = pytest.mark.anyio

CODE_PATTERN = re.compile(r"^\d{6}$")
PK = "receiver-public-key-000000000000000000"
OTHER_PK = "another-public-key-111111111111111111"
WRAPPED = "w" * 88


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
        assert created["sender_secret"]
        assert created["sender_secret"] != created["code"]

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
        assert stored["sender_secret"] == created["sender_secret"]
        view = await _service().get_sender_session(created["session_id"])
        assert "sender_secret" not in view

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
        assert body["sender_secret"]


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


class TestAttachTransfer:
    async def _joined(self) -> dict:
        created = await _service().create_sender_session()
        await _service().join_sender_session(created["code"], PK)
        return created

    async def test_the_sender_stores_the_wrapped_key(self, fake_redis):
        created = await self._joined()

        attached = await _service().attach_transfer(
            created["session_id"],
            "transfer-1",
            WRAPPED,
            sender_secret=created["sender_secret"],
        )

        assert attached["status"] == "attached"
        view = await _service().get_sender_session(created["session_id"])
        assert view["transfer_id"] == "transfer-1"

    async def test_the_mode_travels_with_the_key(self, fake_redis):
        created = await self._joined()

        await _service().attach_transfer(
            created["session_id"],
            "transfer-1",
            WRAPPED,
            "p2p",
            sender_secret=created["sender_secret"],
        )
        collected = await _service().collect_key(created["session_id"])

        assert collected["mode"] == "p2p"

    async def test_attaching_without_a_receiver_is_refused(self, fake_redis):
        created = await _service().create_sender_session()

        with pytest.raises(TransferNotReadyException):
            await _service().attach_transfer(
                created["session_id"],
                "transfer-1",
                WRAPPED,
                sender_secret=created["sender_secret"],
            )

    async def test_attaching_without_the_secret_is_refused(self, fake_redis):
        created = await self._joined()

        with pytest.raises(UnauthorizedException):
            await _service().attach_transfer(
                created["session_id"], "transfer-1", WRAPPED
            )

    async def test_the_wrong_secret_is_refused(self, fake_redis):
        created = await self._joined()

        with pytest.raises(UnauthorizedException):
            await _service().attach_transfer(
                created["session_id"],
                "transfer-1",
                WRAPPED,
                sender_secret="not-the-secret",
            )

    async def test_an_unknown_session_is_refused(self, fake_redis):
        with pytest.raises(TransferNotFoundException):
            await _service().attach_transfer(
                "nope", "transfer-1", WRAPPED, sender_secret="secret"
            )


class TestCollectKey:
    async def _attached(self) -> dict:
        created = await _service().create_sender_session()
        await _service().join_sender_session(created["code"], PK)
        await _service().attach_transfer(
            created["session_id"],
            "transfer-1",
            WRAPPED,
            sender_secret=created["sender_secret"],
        )
        return created

    async def test_the_receiver_collects_the_key_once(self, fake_redis):
        created = await self._attached()

        first = await _service().collect_key(created["session_id"])
        assert first["transfer_id"] == "transfer-1"
        assert first["wrapped_key"] == WRAPPED
        assert first["mode"] == "cloud"

        with pytest.raises(TransferNotReadyException):
            await _service().collect_key(created["session_id"])

    @pytest.mark.parametrize(
        ("status", "expected_mode"),
        [("ready", "cloud"), ("uploading", "p2p"), ("cancelled", "p2p")],
    )
    async def test_cloud_readiness_overrides_a_stale_p2p_hint(
        self, fake_redis, status, expected_mode
    ):
        created = await self._attached()
        await _service().attach_transfer(
            created["session_id"],
            "transfer-1",
            WRAPPED,
            "p2p",
            sender_secret=created["sender_secret"],
        )
        await redis_service.set_transfer(
            "transfer-1",
            {"transfer_id": "transfer-1", "status": status},
            300,
        )
        session_key = f"lynk:sender:{created['session_id']}"
        ttl_before = await fake_redis.ttl(session_key)

        collected = await _service().collect_key(created["session_id"])

        assert collected["mode"] == expected_mode
        assert collected["transfer_id"] == "transfer-1"
        assert collected["wrapped_key"] == WRAPPED
        assert await fake_redis.ttl(session_key) <= ttl_before
        with pytest.raises(TransferNotReadyException):
            await _service().collect_key(created["session_id"])

    async def test_a_failed_readiness_lookup_does_not_consume_the_key(
        self, fake_redis, monkeypatch
    ):
        created = await self._attached()
        await _service().attach_transfer(
            created["session_id"],
            "transfer-1",
            WRAPPED,
            "p2p",
            sender_secret=created["sender_secret"],
        )
        monkeypatch.setattr(
            redis_service,
            "get_transfer",
            AsyncMock(side_effect=ServiceUnavailableException("Redis unavailable")),
        )

        with pytest.raises(ServiceUnavailableException):
            await _service().collect_key(created["session_id"])

        stored = await redis_service.get_sender_session(created["session_id"])
        assert stored["wrapped_key"] == WRAPPED

    async def test_collecting_before_the_wrap_is_refused(self, fake_redis):
        created = await _service().create_sender_session()
        await _service().join_sender_session(created["code"], PK)

        with pytest.raises(TransferNotReadyException):
            await _service().collect_key(created["session_id"])

    async def test_an_unknown_session_is_refused(self, fake_redis):
        with pytest.raises(TransferNotFoundException):
            await _service().collect_key("nope")


class TestPairingRoute:
    async def test_the_sender_attaches_and_the_receiver_collects(self, fake_redis):
        created = await _service().create_sender_session()
        path = created["session_id"]

        with TestClient(app) as client:
            client.post(
                "/api/v1/sender-sessions/join",
                json={"code": created["code"], "receiver_pk": PK},
            )
            attached = client.post(
                f"/api/v1/sender-sessions/{path}/attach",
                json={"transfer_id": "transfer-1", "wrapped_key": WRAPPED},
                headers={"X-Lynk-Sender-Secret": created["sender_secret"]},
            )
            collected = client.get(f"/api/v1/sender-sessions/{path}/key")

        assert attached.status_code == 200
        assert attached.json()["status"] == "attached"
        assert collected.status_code == 200
        assert collected.json()["wrapped_key"] == WRAPPED

    async def test_the_route_refuses_attach_without_the_secret(self, fake_redis):
        created = await _service().create_sender_session()
        await _service().join_sender_session(created["code"], PK)

        with TestClient(app) as client:
            response = client.post(
                f"/api/v1/sender-sessions/{created['session_id']}/attach",
                json={"transfer_id": "transfer-1", "wrapped_key": WRAPPED},
            )

        assert response.status_code == 401
