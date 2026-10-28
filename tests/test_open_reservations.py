"""Open reservations per address (#34): one client can not stack creates.

Time-limiting a reservation stopped an abandoned create from pinning storage
for the whole transfer lifetime, but one address could still open as many as
it liked before any of them lapsed. The address now carries a set of
unfinished transfers and is refused the sixth. Finishing a transfer, or
letting its reservation lapse, frees the slot. Cancelling does not: the
upload URL cannot be revoked, so the slot stays until that signature expires.
"""

import time
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

import app.services.r2_service as r2_mod
from app.core.config import settings
from app.core.exceptions import RateLimitExceededException
from app.services.r2_service import R2Service
from app.services.redis_service import redis_service
from app.services.transfer_service import ACTIVE_TRANSFERS_KEY, TransferService

pytestmark = pytest.mark.anyio

ADDRESS = "203.0.113.7"
OTHER = "198.51.100.9"


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


@pytest.fixture(autouse=True)
def mock_upload_url(monkeypatch):
    monkeypatch.setattr(
        r2_mod.R2Service,
        "generate_upload_url",
        AsyncMock(return_value="https://r2.mock/upload"),
    )


@pytest.fixture(autouse=True)
def mock_verify_file(monkeypatch):
    async def verify(self, transfer_id, file_id):
        data = await redis_service.get_transfer(transfer_id)
        return next(f["file_size"] for f in data["files"] if f["file_id"] == file_id)

    monkeypatch.setattr(r2_mod.R2Service, "verify_file_exists", verify)


def _payload(size: int = 4096) -> list[dict]:
    return [{"file_name": "a.txt", "file_size": size, "content_type": "text/plain"}]


def _service() -> TransferService:
    return TransferService(redis_service=redis_service, r2_service=R2Service())


async def _fill(svc: TransferService, address: str = ADDRESS) -> list[dict]:
    created = []
    for _ in range(settings.MAX_OPEN_RESERVATIONS_PER_CLIENT):
        created.append(await svc.create_transfer(_payload(), client_ip=address))
    return created


class TestOpenReservationCap:
    async def test_the_sixth_open_transfer_is_refused(self, fake_redis):
        svc = _service()
        await _fill(svc)

        with pytest.raises(RateLimitExceededException):
            await svc.create_transfer(_payload(), client_ip=ADDRESS)

    async def test_the_open_set_carries_an_expiry(self, fake_redis):
        svc = _service()
        await svc.create_transfer(_payload(), client_ip=ADDRESS)

        ttl = await fake_redis.ttl(f"lynk:open:{ADDRESS}")
        assert 0 < ttl <= settings.UNFILLED_RESERVATION_SECONDS * 2

    async def test_another_address_is_unaffected(self, fake_redis):
        svc = _service()
        await _fill(svc)

        created = await svc.create_transfer(_payload(), client_ip=OTHER)

        assert created["transfer_id"]
        assert await fake_redis.zcard(f"lynk:open:{OTHER}") == 1


class TestSlotRelease:
    async def test_completing_a_file_frees_the_slot(self, fake_redis):
        svc = _service()
        created = await _fill(svc)

        first = created[0]
        await svc.complete_file(
            transfer_id=first["transfer_id"],
            file_id=first["files"][0]["file_id"],
            sender_secret=first["sender_secret"],
            client_ip=ADDRESS,
        )

        assert await fake_redis.zcard(f"lynk:open:{ADDRESS}") == (
            settings.MAX_OPEN_RESERVATIONS_PER_CLIENT - 1
        )
        fresh = await svc.create_transfer(_payload(), client_ip=ADDRESS)
        assert fresh["transfer_id"]

    async def test_cancelling_keeps_the_slot_until_the_upload_url_expires(
        self, fake_redis, monkeypatch
    ):
        svc = _service()
        delete_objects = AsyncMock()
        monkeypatch.setattr(svc.r2, "delete_objects", delete_objects)
        created = await _fill(svc)

        first = created[0]
        before = int(time.time())
        await svc.cancel_transfer(
            first["transfer_id"],
            sender_secret=first["sender_secret"],
            client_ip=ADDRESS,
        )
        delete_objects.assert_awaited_once_with(
            first["transfer_id"], [first["files"][0]["file_id"]]
        )

        open_key = f"lynk:open:{ADDRESS}"
        assert await fake_redis.zcard(open_key) == settings.MAX_OPEN_RESERVATIONS_PER_CLIENT
        score = float(await fake_redis.zscore(open_key, first["transfer_id"]))
        assert before + settings.UPLOAD_URL_LIFETIME_SECONDS - 2 <= score
        assert score <= before + settings.UPLOAD_URL_LIFETIME_SECONDS + 2
        with pytest.raises(RateLimitExceededException):
            await svc.create_transfer(_payload(), client_ip=ADDRESS)

        await fake_redis.zadd(open_key, {first["transfer_id"]: int(time.time()) - 1})
        fresh = await svc.create_transfer(_payload(), client_ip=ADDRESS)
        assert fresh["transfer_id"]

    async def test_a_cancel_from_another_address_keeps_the_creator_slot(
        self, fake_redis, monkeypatch
    ):
        svc = _service()
        monkeypatch.setattr(svc.r2, "delete_objects", AsyncMock())
        created = await svc.create_transfer(_payload(), client_ip=ADDRESS)

        await svc.cancel_transfer(
            created["transfer_id"],
            sender_secret=created["sender_secret"],
            client_ip=OTHER,
        )

        assert await fake_redis.zcard(f"lynk:open:{ADDRESS}") == 1
        assert await fake_redis.zcard(f"lynk:open:{OTHER}") == 0
        held = await fake_redis.zrange(ACTIVE_TRANSFERS_KEY, 0, -1)
        assert held == [f"{created['transfer_id']}:4096"]

    async def test_a_later_create_does_not_shorten_a_cancelled_slot(
        self, fake_redis, monkeypatch
    ):
        svc = _service()
        monkeypatch.setattr(svc.r2, "delete_objects", AsyncMock())
        cancelled = await svc.create_transfer(_payload(), client_ip=ADDRESS)
        await svc.cancel_transfer(
            cancelled["transfer_id"],
            sender_secret=cancelled["sender_secret"],
            client_ip=ADDRESS,
        )

        await svc.create_transfer(_payload(), client_ip=ADDRESS)

        ttl = int(await fake_redis.ttl(f"lynk:open:{ADDRESS}"))
        assert ttl >= settings.UPLOAD_URL_LIFETIME_SECONDS
        score = float(await fake_redis.zscore(f"lynk:open:{ADDRESS}", cancelled["transfer_id"]))
        assert score >= int(time.time()) + settings.UPLOAD_URL_LIFETIME_SECONDS - 5

    async def test_a_lapsed_reservation_frees_the_slot(self, fake_redis):
        svc = _service()
        created = await _fill(svc)

        # Nothing was uploaded and the two-minute window has passed.
        await fake_redis.zadd(
            f"lynk:open:{ADDRESS}",
            {c["transfer_id"]: int(time.time()) - 1 for c in created},
        )

        assert await svc.create_transfer(_payload(), client_ip=ADDRESS)


class TestNoAddress:
    async def test_a_create_without_an_address_is_not_capped(self, fake_redis):
        svc = _service()
        for _ in range(settings.MAX_OPEN_RESERVATIONS_PER_CLIENT + 1):
            await svc.create_transfer(_payload())

        assert await fake_redis.keys("lynk:open:*") == []
