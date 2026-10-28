"""Capacity reservation (#34): a declared size ties up space only while the
upload is real.

Creating a transfer used to reserve its declared size for the whole transfer
lifetime, so five creates a minute from one address could fill the storage cap
in about four minutes without a single byte uploaded. The reservation now
lapses after a couple of minutes unless a file actually lands, and a real
upload holds capacity until the transfer expires.
"""

import asyncio
import time
from datetime import datetime
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

import app.services.r2_service as r2_mod
from app.core.config import settings
from app.core.exceptions import LimitExceededException
from app.models.transfer import TransferModel
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

    async def test_overlapping_creates_cannot_share_one_slot_of_room(
        self, fake_redis, monkeypatch
    ):
        monkeypatch.setattr(settings, "MAX_TOTAL_R2_CAP_BYTES", 5000)
        svc = TransferService(redis_service=redis_service, r2_service=R2Service())
        started = asyncio.Event()
        release = asyncio.Event()
        releases = 0
        real_release = redis_service.release_record_lock

        async def stall(client, lock_key, token):
            nonlocal releases
            if lock_key == f"{KEY}:lock":
                releases += 1
                if releases == 1:
                    started.set()
                    await release.wait()
            await real_release(client, lock_key, token)

        monkeypatch.setattr(redis_service, "release_record_lock", stall)
        first = asyncio.create_task(svc.create_transfer(_payload()))
        await asyncio.wait_for(started.wait(), timeout=2)
        second = asyncio.create_task(svc.create_transfer(_payload()))
        await asyncio.sleep(0.05)
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)

        assert sum(isinstance(item, LimitExceededException) for item in results) == 1
        assert sum(isinstance(item, dict) for item in results) == 1


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

    async def test_a_refused_extend_keeps_the_original_lifetime(self, fake_redis):
        svc = TransferService(redis_service=redis_service, r2_service=R2Service())
        created = await svc.create_transfer(_two_files())
        tid = created["transfer_id"]
        key = redis_service._get_key(tid)
        before = await redis_service.get_transfer(tid)
        now = int(time.time())
        await fake_redis.zadd(KEY, {f"{tid}:50000001": now - 5})
        await fake_redis.zadd(
            KEY,
            {f"other:{settings.MAX_TOTAL_R2_CAP_BYTES + 1}": now + 600},
        )

        with pytest.raises(LimitExceededException):
            await svc.extend_transfer(tid, sender_secret=created["sender_secret"])

        stored = await redis_service.get_transfer(tid)
        assert stored["expires_at"] == before["expires_at"]
        ttl = int(await fake_redis.ttl(key))
        assert 0 < ttl <= settings.TRANSFER_LIFETIME_SECONDS
        assert ttl < settings.EXTENDED_TRANSFER_LIFETIME_SECONDS

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

        before = int(time.time())
        await svc.cancel_transfer(tid, sender_secret=created["sender_secret"])

        held = await fake_redis.zrange(KEY, 0, -1, withscores=True)
        assert held == [(f"{tid}:50000001", held[0][1])]
        assert before + settings.UPLOAD_URL_LIFETIME_SECONDS - 2 <= held[0][1]
        assert held[0][1] <= before + settings.UPLOAD_URL_LIFETIME_SECONDS + 2

    async def test_cancel_drops_a_member_that_appeared_after_the_snapshot(
        self, fake_redis, monkeypatch
    ):
        monkeypatch.setattr(r2_mod.R2Service, "delete_objects", AsyncMock())
        svc = TransferService(redis_service=redis_service, r2_service=R2Service())
        created = await svc.create_transfer(_payload())
        tid = created["transfer_id"]
        await fake_redis.zadd(KEY, {f"{tid}:12345": int(time.time()) + 600})

        await svc.cancel_transfer(tid, sender_secret=created["sender_secret"])

        remaining = await fake_redis.zrange(KEY, 0, -1)
        assert remaining == [f"{tid}:4096"]

    async def test_a_hold_after_cancel_writes_nothing(self, fake_redis, monkeypatch):
        monkeypatch.setattr(r2_mod.R2Service, "delete_objects", AsyncMock())
        svc = TransferService(redis_service=redis_service, r2_service=R2Service())
        created = await svc.create_transfer(_payload())
        tid = created["transfer_id"]
        stale = TransferModel.model_validate(await redis_service.get_transfer(tid))

        await svc.cancel_transfer(tid, sender_secret=created["sender_secret"])
        before = dict(await fake_redis.zrange(KEY, 0, -1, withscores=True))
        held = await svc._hold_landed_bytes(stale, 4096, int(time.time()) + 600)

        assert held is None
        assert dict(await fake_redis.zrange(KEY, 0, -1, withscores=True)) == before


class TestCompleteRechecksTheCap:
    async def test_a_late_upload_is_refused_once_the_reservation_is_gone(
        self, fake_redis, monkeypatch
    ):
        _verify_as_declared(monkeypatch)
        deleted = AsyncMock()
        monkeypatch.setattr(r2_mod.R2Service, "delete_objects", deleted)
        svc = TransferService(redis_service=redis_service, r2_service=R2Service())
        created = await svc.create_transfer(_payload())
        tid = created["transfer_id"]
        await fake_redis.zrem(KEY, f"{tid}:4096")
        # One member already fills the cap, so this upload has nowhere to land.
        await fake_redis.zadd(
            KEY,
            {f"other:{settings.MAX_TOTAL_R2_CAP_BYTES}": int(time.time()) + 600},
        )

        with pytest.raises(LimitExceededException):
            await svc.complete_file(
                transfer_id=tid,
                file_id=created["files"][0]["file_id"],
                sender_secret=created["sender_secret"],
            )

        stored = await redis_service.get_transfer(tid)
        assert stored["files"][0]["status"] != "uploaded"
        assert stored["status"] != "ready"
        deleted.assert_awaited()

    async def test_an_expired_reservation_does_not_skip_the_cap(
        self, fake_redis, monkeypatch
    ):
        _verify_as_declared(monkeypatch)
        deleted = AsyncMock()
        monkeypatch.setattr(r2_mod.R2Service, "delete_objects", deleted)
        svc = TransferService(redis_service=redis_service, r2_service=R2Service())
        created = await svc.create_transfer(_two_files())
        tid = created["transfer_id"]
        now = int(time.time())
        await fake_redis.zadd(KEY, {f"{tid}:50000001": now - 5})
        await fake_redis.zadd(
            KEY, {f"other:{settings.MAX_TOTAL_R2_CAP_BYTES}": now + 600}
        )
        tiny = next(f for f in created["files"] if f["file_name"] == "tiny.txt")

        with pytest.raises(LimitExceededException):
            await svc.complete_file(
                transfer_id=tid,
                file_id=tiny["file_id"],
                sender_secret=created["sender_secret"],
            )

        stored = await redis_service.get_transfer(tid)
        tiny_stored = next(f for f in stored["files"] if f["file_name"] == "tiny.txt")
        assert tiny_stored["status"] != "uploaded"
        deleted.assert_awaited()
