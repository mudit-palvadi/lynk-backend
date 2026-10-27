"""Concurrent JSON updates must keep every field the other writer changed."""

import asyncio
import json
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from app.core.config import settings
from app.core.exceptions import TransferNotFoundException, TransferNotReadyException
from app.services.r2_service import R2Service
from app.services.receiver_service import ReceiverService
from app.services.redis_service import redis_service
from app.services.sender_service import SenderService
from app.services.transfer_service import TransferService

pytestmark = pytest.mark.anyio


@pytest.fixture()
async def fake_redis():
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    original = redis_service.get_client

    async def _get_client():
        return fake

    redis_service.get_client = _get_client
    yield fake
    redis_service.get_client = original
    await fake.aclose()


async def test_two_writers_keep_both_fields(fake_redis):
    await fake_redis.set("lynk:transfer:t-1", json.dumps({"files": 0, "life": 0}), ex=60)

    async def bump(field: str) -> None:
        def mutate(data: dict) -> dict:
            data[field] += 1
            return data

        await redis_service.mutate_json("lynk:transfer:t-1", mutate)

    await asyncio.gather(bump("files"), bump("life"))

    stored = json.loads(await fake_redis.get("lynk:transfer:t-1"))
    assert stored == {"files": 1, "life": 1}


async def test_a_finished_writer_does_not_delete_the_next_lock(fake_redis, monkeypatch):
    await fake_redis.set("lynk:transfer:t-1", json.dumps({"n": 0}), ex=60)
    original_release = redis_service.release_record_lock

    async def release_after_takeover(client, lock_key, token):
        # The first writer's lock expired and the next writer already owns it.
        await client.set(lock_key, "next-writer")
        await original_release(client, lock_key, token)

    monkeypatch.setattr(redis_service, "release_record_lock", release_after_takeover)

    def mutate(data: dict) -> dict:
        data["n"] += 1
        return data

    await redis_service.mutate_json("lynk:transfer:t-1", mutate)

    assert await fake_redis.get("lynk:transfer:t-1:lock") == "next-writer"


async def test_a_completion_cannot_restore_a_cancelled_transfer(fake_redis, monkeypatch):
    monkeypatch.setattr(
        R2Service,
        "generate_upload_url",
        AsyncMock(return_value="https://r2.mock/upload"),
    )
    monkeypatch.setattr(R2Service, "verify_file_exists", AsyncMock(return_value=4096))
    monkeypatch.setattr(R2Service, "delete_objects", AsyncMock())
    service = TransferService(redis_service=redis_service, r2_service=R2Service())
    created = await service.create_transfer(
        [{"file_name": "a.txt", "file_size": 4096, "content_type": "text/plain"}]
    )
    held = asyncio.Event()
    resume = asyncio.Event()
    original = redis_service.acquire_record_lock

    async def pause_the_cancel(client, lock_key):
        token = await original(client, lock_key)
        if not pause_the_cancel.used:
            pause_the_cancel.used = True
            held.set()
            await resume.wait()
        return token

    pause_the_cancel.used = False
    monkeypatch.setattr(redis_service, "acquire_record_lock", pause_the_cancel)

    cancel_task = asyncio.create_task(
        service.cancel_transfer(
            created["transfer_id"],
            sender_secret=created["sender_secret"],
        )
    )
    await held.wait()
    complete_task = asyncio.create_task(
        service.complete_file(
            created["transfer_id"],
            created["files"][0]["file_id"],
            sender_secret=created["sender_secret"],
        )
    )
    await asyncio.sleep(0.05)
    resume.set()
    await cancel_task
    with pytest.raises(TransferNotFoundException):
        await complete_task
    assert await fake_redis.get(redis_service._get_key(created["transfer_id"])) is None


async def test_two_sender_collects_return_the_key_once(fake_redis):
    service = SenderService(redis_service)
    created = await service.create_sender_session()
    await service.join_sender_session(created["code"], "k" * 32)
    await redis_service.set_transfer(
        "transfer-1",
        {"transfer_id": "transfer-1", "status": "uploading"},
        300,
    )
    await service.attach_transfer(
        created["session_id"],
        "transfer-1",
        "w" * 32,
        sender_secret=created["sender_secret"],
    )

    results = await asyncio.gather(
        service.collect_key(created["session_id"]),
        service.collect_key(created["session_id"]),
        return_exceptions=True,
    )

    delivered = [item["wrapped_key"] for item in results if isinstance(item, dict)]
    refused = [item for item in results if isinstance(item, TransferNotReadyException)]
    assert delivered == ["w" * 32]
    assert len(refused) == 1


async def test_two_receiver_reads_return_the_key_once(fake_redis):
    service = ReceiverService(redis_service)
    created = await service.create_receiver_session()
    await fake_redis.set(
        "lynk:transfer:t-1",
        json.dumps(
            {
                "transfer_id": "t-1",
                "status": "ready",
                "created_at": "2026-10-27T00:00:00+00:00",
                "expires_at": "2026-10-27T00:10:00+00:00",
                "total_files": 1,
                "total_size": 1,
                "files": [],
            }
        ),
        ex=60,
    )
    await service.attach_transfer(created["session_id"], "t-1", "w" * 32)

    results = await asyncio.gather(
        service.get_receiver_session(created["session_id"]),
        service.get_receiver_session(created["session_id"]),
    )

    delivered = [item["wrapped_key"] for item in results if item["wrapped_key"]]
    assert delivered == ["w" * 32]


async def test_a_second_attach_cannot_replace_or_restore_the_key(fake_redis):
    service = ReceiverService(redis_service)
    created = await service.create_receiver_session()
    await fake_redis.set(
        "lynk:transfer:t-1",
        json.dumps(
            {
                "transfer_id": "t-1",
                "status": "ready",
                "created_at": "2026-10-27T00:00:00+00:00",
                "expires_at": "2026-10-27T00:10:00+00:00",
                "total_files": 1,
                "total_size": 1,
                "files": [],
            }
        ),
        ex=60,
    )
    await service.attach_transfer(created["session_id"], "t-1", "first-key")

    with pytest.raises(TransferNotReadyException):
        await service.attach_transfer(created["session_id"], "t-1", "other-key")

    delivered = await service.get_receiver_session(created["session_id"])
    assert delivered["wrapped_key"] == "first-key"

    with pytest.raises(TransferNotReadyException):
        await service.attach_transfer(created["session_id"], "t-1", "first-key")

    stored = json.loads(
        await fake_redis.get(redis_service._get_receiver_key(created["session_id"]))
    )
    assert not stored["wrapped_key"]


async def test_extend_keeps_a_file_completed_at_the_same_time(fake_redis, monkeypatch):
    monkeypatch.setattr(
        R2Service,
        "generate_upload_url",
        AsyncMock(return_value="https://r2.mock/upload"),
    )
    monkeypatch.setattr(R2Service, "verify_file_exists", AsyncMock(return_value=4096))
    service = TransferService(redis_service=redis_service, r2_service=R2Service())
    created = await service.create_transfer(
        [{"file_name": "a.txt", "file_size": 4096, "content_type": "text/plain"}]
    )

    await asyncio.gather(
        service.complete_file(
            created["transfer_id"],
            created["files"][0]["file_id"],
            sender_secret=created["sender_secret"],
        ),
        service.extend_transfer(
            created["transfer_id"], sender_secret=created["sender_secret"]
        ),
    )

    stored = json.loads(await fake_redis.get(f"lynk:transfer:{created['transfer_id']}"))
    assert stored["files"][0]["status"] == "uploaded"
    assert stored["status"] == "ready"
    ttl = await fake_redis.ttl(f"lynk:transfer:{created['transfer_id']}")
    assert ttl > settings.TRANSFER_LIFETIME_SECONDS
