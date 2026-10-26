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


def _two_files() -> list[dict]:
    return [
        {"file_name": "tiny.txt", "file_size": 1, "content_type": "text/plain"},
        {
            "file_name": "big.bin",
            "file_size": 50_000_000,
            "content_type": "application/octet-stream",
        },
    ]


def _verify_as_declared(monkeypatch) -> None:
    async def verify(self, transfer_id, file_id):
        data = await redis_service.get_transfer(transfer_id)
        return next(f["file_size"] for f in data["files"] if f["file_id"] == file_id)

    monkeypatch.setattr(r2_mod.R2Service, "verify_file_exists", verify)


class TestOnlyLandedBytesOutliveTheReservation:
    """#50: one address could hold the whole cap without uploading it."""

    async def test_extending_an_empty_transfer_holds_no_capacity(self, fake_redis):
        svc = TransferService(redis_service=redis_service, r2_service=R2Service())
        before = int(time.time())
        created = await svc.create_transfer(_two_files())
        tid = created["transfer_id"]

        await svc.extend_transfer(tid, sender_secret=created["sender_secret"])

        members = await fake_redis.zrange(KEY, 0, -1, withscores=True)
        assert members == [(f"{tid}:50000001", members[0][1])]
        # Still the short create-time reservation, not the 30-minute extension.
        assert members[0][1] <= before + settings.UNFILLED_RESERVATION_SECONDS + 2

    async def test_one_small_file_holds_only_its_own_bytes(self, fake_redis, monkeypatch):
        _verify_as_declared(monkeypatch)
        svc = TransferService(redis_service=redis_service, r2_service=R2Service())
        before = int(time.time())
        created = await svc.create_transfer(_two_files(), client_ip="203.0.113.9")
        tid = created["transfer_id"]
        tiny = next(f for f in created["files"] if f["file_name"] == "tiny.txt")

        await svc.complete_file(
            transfer_id=tid,
            file_id=tiny["file_id"],
            sender_secret=created["sender_secret"],
            client_ip="203.0.113.9",
        )
        await svc.extend_transfer(tid, sender_secret=created["sender_secret"])

        held = dict(await fake_redis.zrange(KEY, 0, -1, withscores=True))
        assert held[f"{tid}:50000001"] <= before + settings.UNFILLED_RESERVATION_SECONDS + 2
        assert held[f"{tid}:1"] > before + settings.UNFILLED_RESERVATION_SECONDS
        # The address keeps its open slot until the transfer is ready.
        assert await fake_redis.zcard("lynk:open:203.0.113.9") == 1

    async def test_the_hold_grows_with_each_file_and_ends_as_one_member(
        self, fake_redis, monkeypatch
    ):
        _verify_as_declared(monkeypatch)
        svc = TransferService(redis_service=redis_service, r2_service=R2Service())
        created = await svc.create_transfer(_two_files(), client_ip="203.0.113.9")
        tid = created["transfer_id"]

        for f in created["files"]:
            await svc.complete_file(
                transfer_id=tid,
                file_id=f["file_id"],
                sender_secret=created["sender_secret"],
                client_ip="203.0.113.9",
            )

        data = await redis_service.get_transfer(tid)
        expected = int(datetime.fromisoformat(data["expires_at"]).timestamp())
        assert await fake_redis.zrange(KEY, 0, -1, withscores=True) == [
            (f"{tid}:50000001", expected)
        ]
        assert await fake_redis.zcard("lynk:open:203.0.113.9") == 0

    async def test_cancel_drops_the_reservation_and_the_landed_bytes(
        self, fake_redis, monkeypatch
    ):
        _verify_as_declared(monkeypatch)
        monkeypatch.setattr(r2_mod.R2Service, "delete_objects", AsyncMock())
        svc = TransferService(redis_service=redis_service, r2_service=R2Service())
        created = await svc.create_transfer(_two_files())
        tid = created["transfer_id"]
        tiny = next(f for f in created["files"] if f["file_name"] == "tiny.txt")
        await svc.complete_file(
            transfer_id=tid, file_id=tiny["file_id"], sender_secret=created["sender_secret"]
        )

        await svc.cancel_transfer(tid, sender_secret=created["sender_secret"])

        assert await fake_redis.zrange(KEY, 0, -1) == []
