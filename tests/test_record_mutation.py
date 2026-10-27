"""Concurrent JSON updates must keep every field the other writer changed."""

import asyncio
import json

import fakeredis.aioredis
import pytest

from app.services.redis_service import redis_service

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
