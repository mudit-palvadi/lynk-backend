# Redis service placeholder
import asyncio
import json
import logging
import secrets
import redis.asyncio as aioredis
from app.core.config import settings
from app.core.exceptions import ServiceUnavailableException, TransferNotFoundException

logger = logging.getLogger(__name__)


class RedisService:
    def __init__(self) -> None:
        self.pool: aioredis.ConnectionPool | None = None

    def init_pool(self) -> None:
        """Initializes the connection pool. Called during app startup."""
        try:
            self.pool = aioredis.ConnectionPool.from_url(
                settings.REDIS_URL, decode_responses=True, encoding="utf-8"
            )
            logger.info("Redis connection pool initialized.")
        except Exception as e:
            logger.error(f"Failed to create Redis pool: {e}")
            raise ServiceUnavailableException("Failed to initialize Redis connection pool")

    async def get_client(self) -> aioredis.Redis:
        """Helper to get a client instance from the pool."""
        if not self.pool:
            raise ServiceUnavailableException("Redis connection pool is not initialized")
        return aioredis.Redis(connection_pool=self.pool)

    async def mutate_json(
        self,
        key: str,
        mutate,
        *,
        ttl_seconds: int | None = None,
    ) -> dict | None:
        """Change one JSON value without dropping a concurrent edit.

        A short exclusive lock makes two writers apply in order. Each one
        reads the value the previous writer stored, so a file-status update
        and a lifetime extension both survive.
        """
        client = await self.get_client()
        lock_key = f"{key}:lock"
        token = None
        try:
            token = await self.acquire_record_lock(client, lock_key)

            raw = await client.get(key)
            if not raw:
                return None
            updated = mutate(json.loads(raw))
            if not isinstance(updated, dict):
                raise ServiceUnavailableException("Failed to update the record")

            payload = json.dumps(updated)
            if ttl_seconds is None:
                ttl = await client.ttl(key)
                if ttl is None or ttl <= 0:
                    raise TransferNotFoundException()
                await client.set(key, payload, ex=int(ttl))
            else:
                await client.set(key, payload, ex=ttl_seconds)
            return updated
        finally:
            if token is not None:
                await self.release_record_lock(client, lock_key, token)

    async def acquire_record_lock(self, client, lock_key: str) -> str:
        """Take the short exclusive lock and return the token that owns it.

        The token is random. A writer that runs past the lock expiry must not
        delete the lock a later writer has already taken.
        """
        token = secrets.token_hex(16)
        for _ in range(40):
            if await client.set(lock_key, token, nx=True, px=3000):
                return token
            await asyncio.sleep(0.02)
        raise ServiceUnavailableException("Failed to update the record")

    async def release_record_lock(self, client, lock_key: str, token: str) -> None:
        """Drop the lock only when this writer still holds it."""
        current = await client.get(lock_key)
        if current == token:
            await client.delete(lock_key)

    async def _take_wrapped_key(self, key: str) -> str | None:
        """Return the wrapped key once, and clear it before anyone else can."""
        taken: dict[str, str | None] = {"value": None}

        def mutate(data: dict) -> dict:
            if data.get("status") == "attached" and data.get("wrapped_key"):
                taken["value"] = data["wrapped_key"]
                data["wrapped_key"] = None
            return data

        await self.mutate_json(key, mutate)
        return taken["value"]

    async def take_receiver_wrapped_key(self, session_id: str) -> str | None:
        return await self._take_wrapped_key(self._get_receiver_key(session_id))

    async def take_sender_wrapped_key(self, session_id: str) -> str | None:
        return await self._take_wrapped_key(self._get_sender_key(session_id))

    def _get_key(self, transfer_id: str) -> str:
        return f"lynk:transfer:{transfer_id}"

    async def set_transfer(self, transfer_id: str, data: dict, ttl_seconds: int) -> None:
        """Stores transfer metadata with a specific TTL (time-to-live) in seconds."""
        client = await self.get_client()
        key = self._get_key(transfer_id)
        try:
            val = json.dumps(data)
            await client.setex(key, ttl_seconds, val)
        except Exception as e:
            logger.error(f"Redis set_transfer failed: {e}")
            raise ServiceUnavailableException("Failed to save transfer metadata")

    async def get_transfer(self, transfer_id: str) -> dict | None:
        """Retrieves transfer metadata. Returns None if expired or not found."""
        client = await self.get_client()
        key = self._get_key(transfer_id)
        try:
            val = await client.get(key)
            if not val:
                return None
            return json.loads(val)
        except Exception as e:
            logger.error(f"Redis get_transfer failed: {e}")
            raise ServiceUnavailableException("Failed to retrieve transfer metadata")

    async def update_transfer(self, transfer_id: str, data: dict) -> None:
        """
        Updates transfer metadata while preserving the remaining TTL.
        Raises TransferNotFoundException if the transfer has expired or does not exist.
        """
        client = await self.get_client()
        key = self._get_key(transfer_id)
        try:
            # 1. Read current TTL to prevent extending the lifetime
            ttl = await client.ttl(key)

            # Redis TTL returns -2 if key does not exist
            if ttl == -2:
                raise TransferNotFoundException()

            # Handle edge cases (no expiry key -1 or expired keys)
            if ttl <= 0:
                if ttl == -1:
                    ttl = settings.TRANSFER_LIFETIME_SECONDS
                else:
                    raise TransferNotFoundException()

            # 2. Write the updated data preserving the remaining seconds
            val = json.dumps(data)
            await client.setex(key, ttl, val)
        except TransferNotFoundException:
            raise
        except Exception as e:
            logger.error(f"Redis update_transfer failed: {e}")
            raise ServiceUnavailableException("Failed to update transfer metadata")

    async def delete_transfer(self, transfer_id: str) -> None:
        """Remove a transfer while holding the same lock as an in-place edit.

        A completion that is still outside the lock then sees a missing record
        and stops, instead of writing the record back after this delete.
        """
        client = await self.get_client()
        key = self._get_key(transfer_id)
        token = None
        try:
            token = await self.acquire_record_lock(client, f"{key}:lock")
            await client.delete(key)
        except ServiceUnavailableException:
            raise
        except Exception as e:
            logger.error(f"Redis delete_transfer failed: {e}")
            raise ServiceUnavailableException("Failed to delete transfer metadata")
        finally:
            if token is not None:
                await self.release_record_lock(client, key + ":lock", token)

    async def extend_transfer(self, transfer_id: str, data: dict, ttl_seconds: int) -> None:
        """
        Extends a transfer's lifetime to ttl_seconds (Rewarded Ad unlock).
        Raises TransferNotFoundException if the transfer has expired or does not exist.
        """
        client = await self.get_client()
        key = self._get_key(transfer_id)
        try:
            exists = await client.exists(key)
            if not exists:
                raise TransferNotFoundException()
            val = json.dumps(data)
            await client.setex(key, ttl_seconds, val)
        except TransferNotFoundException:
            raise
        except Exception as e:
            logger.error(f"Redis extend_transfer failed: {e}")
            raise ServiceUnavailableException("Failed to extend transfer lifetime")

    def _get_receiver_key(self, session_id: str) -> str:
        return f"lynk:receiver:{session_id}"

    async def set_receiver_session(self, session_id: str, data: dict, ttl_seconds: int) -> None:
        client = await self.get_client()
        key = self._get_receiver_key(session_id)
        try:
            val = json.dumps(data)
            await client.setex(key, ttl_seconds, val)
        except Exception as e:
            logger.error(f"Redis set_receiver_session failed: {e}")
            raise ServiceUnavailableException("Failed to save receiver session")

    async def get_receiver_session(self, session_id: str) -> dict | None:
        client = await self.get_client()
        key = self._get_receiver_key(session_id)
        try:
            val = await client.get(key)
            if not val:
                return None
            return json.loads(val)
        except Exception as e:
            logger.error(f"Redis get_receiver_session failed: {e}")
            raise ServiceUnavailableException("Failed to retrieve receiver session")

    async def update_receiver_session(self, session_id: str, data: dict) -> None:
        client = await self.get_client()
        key = self._get_receiver_key(session_id)
        try:
            ttl = await client.ttl(key)
            if ttl == -2:
                raise TransferNotFoundException("Receiver session not found or expired")
            if ttl <= 0:
                if ttl == -1:
                    ttl = settings.RECEIVER_SESSION_LIFETIME_SECONDS
                else:
                    raise TransferNotFoundException("Receiver session not found or expired")

            val = json.dumps(data)
            await client.setex(key, ttl, val)
        except TransferNotFoundException:
            raise
        except Exception as e:
            logger.error(f"Redis update_receiver_session failed: {e}")
            raise ServiceUnavailableException("Failed to update receiver session")

    async def delete_receiver_session(self, session_id: str) -> None:
        """Remove a receiver session while holding the same lock as attach.

        An attach that already read the session then sees a missing record
        and stops, instead of writing the session back after this delete.
        """
        client = await self.get_client()
        key = self._get_receiver_key(session_id)
        token = None
        try:
            token = await self.acquire_record_lock(client, f"{key}:lock")
            await client.delete(key)
        except ServiceUnavailableException:
            raise
        except Exception as e:
            logger.error(f"Redis delete_receiver_session failed: {e}")
            raise ServiceUnavailableException("Failed to delete receiver session")
        finally:
            if token is not None:
                await self.release_record_lock(client, f"{key}:lock", token)

    def _get_sender_key(self, session_id: str) -> str:
        return f"lynk:sender:{session_id}"

    def _get_sender_code_key(self, code: str) -> str:
        return f"lynk:sender_code:{code}"

    async def claim_sender_code(
        self, code: str, session_id: str, ttl_seconds: int
    ) -> bool:
        """Takes the code for this session. False when another session holds it."""
        client = await self.get_client()
        key = self._get_sender_code_key(code)
        try:
            return bool(await client.set(key, session_id, nx=True, ex=ttl_seconds))
        except Exception as e:
            logger.error(f"Redis claim_sender_code failed: {e}")
            raise ServiceUnavailableException("Failed to claim a session code")

    async def claim_sender_receiver(
        self, session_id: str, receiver_pk: str, ttl_seconds: int
    ) -> str:
        """The first public key claims the session. Later keys cannot replace it.

        Returns ``claimed`` for the first key, ``same`` when that key joins
        again, and ``other`` when a different key tries to take the session.
        """
        client = await self.get_client()
        key = f"lynk:sender_receiver:{session_id}"
        try:
            if ttl_seconds <= 0:
                return "other"
            if await client.set(key, receiver_pk, nx=True, ex=ttl_seconds):
                return "claimed"
            current = await client.get(key)
            if current == receiver_pk:
                return "same"
            return "other"
        except Exception as e:
            logger.error(f"Redis claim_sender_receiver failed: {e}")
            raise ServiceUnavailableException("Failed to claim the receiver")

    async def get_sender_code(self, code: str) -> str | None:
        """Returns the session id a code names, or None once it has expired."""
        client = await self.get_client()
        key = self._get_sender_code_key(code)
        try:
            return await client.get(key)
        except Exception as e:
            logger.error(f"Redis get_sender_code failed: {e}")
            raise ServiceUnavailableException("Failed to look up a session code")

    async def set_sender_session(self, session_id: str, data: dict, ttl_seconds: int) -> None:
        client = await self.get_client()
        key = self._get_sender_key(session_id)
        try:
            val = json.dumps(data)
            await client.setex(key, ttl_seconds, val)
        except Exception as e:
            logger.error(f"Redis set_sender_session failed: {e}")
            raise ServiceUnavailableException("Failed to save sender session")

    async def get_sender_session(self, session_id: str) -> dict | None:
        client = await self.get_client()
        key = self._get_sender_key(session_id)
        try:
            val = await client.get(key)
            if not val:
                return None
            return json.loads(val)
        except Exception as e:
            logger.error(f"Redis get_sender_session failed: {e}")
            raise ServiceUnavailableException("Failed to retrieve sender session")

    async def update_sender_session(self, session_id: str, data: dict) -> None:
        client = await self.get_client()
        key = self._get_sender_key(session_id)
        try:
            ttl = await client.ttl(key)
            if ttl == -2:
                raise TransferNotFoundException("Sender session not found or expired")
            if ttl <= 0:
                if ttl == -1:
                    ttl = settings.SENDER_SESSION_LIFETIME_SECONDS
                else:
                    raise TransferNotFoundException("Sender session not found or expired")

            val = json.dumps(data)
            await client.setex(key, ttl, val)
        except TransferNotFoundException:
            raise
        except Exception as e:
            logger.error(f"Redis update_sender_session failed: {e}")
            raise ServiceUnavailableException("Failed to update sender session")

    async def delete_sender_session(self, session_id: str) -> None:
        client = await self.get_client()
        key = self._get_sender_key(session_id)
        try:
            await client.delete(key)
        except Exception as e:
            logger.error(f"Redis delete_sender_session failed: {e}")
            raise ServiceUnavailableException("Failed to delete sender session")

    async def close_pool(self) -> None:
        if self.pool:
            await self.pool.disconnect()
            logger.info("Redis connection pool closed.")
            self.pool = None


redis_service = RedisService()
