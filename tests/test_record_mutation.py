"""Concurrent JSON updates must keep every field the other writer changed."""

import asyncio
import json

import fakeredis.aioredis
import pytest

from app.core.exceptions import TransferNotReadyException
from app.services.receiver_service import ReceiverService
from app.services.redis_service import redis_service
from app.services.sender_service import SenderService

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


async def test_two_sender_collects_return_the_key_once(fake_redis):
    service = SenderService(redis_service)
    created = await service.create_sender_session()
    await service.join_sender_session(created["code"], "k" * 32)
    await service.attach_transfer(created["session_id"], "transfer-1", "w" * 32)

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
