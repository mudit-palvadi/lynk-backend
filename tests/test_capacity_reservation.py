"""Capacity reservation (#34): a declared size ties up space only while the
upload is real.

Creating a transfer used to reserve its declared size for the whole transfer
lifetime, so five creates a minute from one address could fill the storage cap
in about four minutes without a single byte uploaded. The reservation now
lapses after a couple of minutes unless a file actually lands, and a real
upload holds capacity until the transfer expires.
"""

import time
from datetime import datetime
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

import app.services.r2_service as r2_mod
from app.core.config import settings
from app.services.r2_service import R2Service
from app.services.redis_service import redis_service
from app.services.transfer_service import TransferService

pytestmark = pytest.mark.anyio

KEY = "lynk:active_transfers"


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


def _payload(size: int = 4096) -> list[dict]:
    return [{"file_name": "a.txt", "file_size": size, "content_type": "text/plain"}]


async def _reservation(fake, member: str) -> int | None:
    members = await fake.zrange(KEY, 0, -1, withscores=True)
    return {name: int(score) for name, score in members}.get(member)


class TestUnfilledReservation:
    async def test_a_create_reserves_capacity_for_minutes_only(self, fake_redis):
        svc = TransferService(redis_service=redis_service, r2_service=R2Service())
        before = int(time.time())

        created = await svc.create_transfer(_payload())
        member = f"{created['transfer_id']}:4096"
        score = await _reservation(fake_redis, member)

        # A couple of minutes, not the ten-minute transfer lifetime.
        assert score is not None
        assert score <= before + settings.UNFILLED_RESERVATION_SECONDS + 2
        assert score < before + settings.TRANSFER_LIFETIME_SECONDS

    async def test_a_lapsed_reservation_is_freed_by_the_next_create(self, fake_redis):
        svc = TransferService(redis_service=redis_service, r2_service=R2Service())
        stale = await svc.create_transfer(_payload())
        stale_member = f"{stale['transfer_id']}:4096"
        # Time passes and nothing is uploaded.
        await fake_redis.zadd(KEY, {stale_member: int(time.time()) - 1})

        fresh = await svc.create_transfer(_payload())

        remaining = await fake_redis.zrange(KEY, 0, -1)
        assert stale_member not in remaining
        assert f"{fresh['transfer_id']}:4096" in remaining


class TestFilledReservation:
    async def test_a_real_upload_holds_capacity_until_expiry(self, fake_redis, monkeypatch):
        async def verify(self, transfer_id, file_id):
            data = await redis_service.get_transfer(transfer_id)
            return next(f["file_size"] for f in data["files"] if f["file_id"] == file_id)

        monkeypatch.setattr(r2_mod.R2Service, "verify_file_exists", verify)

        svc = TransferService(redis_service=redis_service, r2_service=R2Service())
        created = await svc.create_transfer(_payload())
        tid = created["transfer_id"]
        member = f"{tid}:4096"

        await svc.complete_file(
            transfer_id=tid,
            file_id=created["files"][0]["file_id"],
            sender_secret=created["sender_secret"],
        )

        data = await redis_service.get_transfer(tid)
        expected = int(datetime.fromisoformat(data["expires_at"]).timestamp())
        assert await _reservation(fake_redis, member) == expected
        assert expected > int(time.time()) + settings.UNFILLED_RESERVATION_SECONDS
