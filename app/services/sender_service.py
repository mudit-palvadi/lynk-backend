import asyncio
import json
import secrets
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.exceptions import (
    ServiceUnavailableException,
    TransferNotFoundException,
    TransferNotReadyException,
    UnauthorizedException,
)
from app.core.security import secrets_match
from app.models.sender import SenderSessionModel
from app.services.redis_service import RedisService


class SenderService:
    """Sender-owned pairing sessions addressed by a six-digit code.

    The code only names the session; it never carries a key. The receiver
    publishes its public key here and the sender seals the AES key to it, the
    same way the receiver-QR pairing works in the other direction.
    """

    MAX_CODE_ATTEMPTS = 20

    def __init__(self, redis_service: RedisService) -> None:
        self.redis = redis_service

    def _generate_id(self, length: int = 12) -> str:
        return secrets.token_urlsafe(length)[:length]

    def _generate_code(self) -> str:
        digits = settings.SENDER_CODE_LENGTH
        return f"{secrets.randbelow(10**digits):0{digits}d}"

    async def create_sender_session(self) -> dict:
        ttl = settings.SENDER_SESSION_LIFETIME_SECONDS
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=ttl)).isoformat()
        session_id = self._generate_id()
        sender_secret = secrets.token_urlsafe(32)

        for _ in range(self.MAX_CODE_ATTEMPTS):
            code = self._generate_code()
            # The code lives exactly as long as the session it names, and the
            # claim is atomic so two senders never share a code.
            if not await self.redis.claim_sender_code(code, session_id, ttl):
                continue

            session = SenderSessionModel(
                session_id=session_id,
                code=code,
                sender_secret=sender_secret,
                status="waiting",
                created_at=now.isoformat(),
                expires_at=expires_at,
            )
            await self.redis.set_sender_session(session_id, session.model_dump(), ttl)

            return {
                "session_id": session_id,
                "code": code,
                "status": "waiting",
                "expires_in": ttl,
                "sender_secret": sender_secret,
            }

        raise ServiceUnavailableException("Could not allocate a session code")

    async def _ttl(self, session_id: str) -> int:
        client = await self.redis.get_client()
        key = self.redis._get_sender_key(session_id)
        ttl = await client.ttl(key)
        return max(0, ttl) if ttl != -2 else 0

    async def _edit_session(self, session_id: str, mutate) -> dict:
        """Apply one session change without dropping a concurrent edit."""
        client = await self.redis.get_client()
        key = self.redis._get_sender_key(session_id)
        lock_key = f"{key}:lock"
        acquired = False
        try:
            for _ in range(40):
                acquired = bool(await client.set(lock_key, "1", nx=True, px=3000))
                if acquired:
                    break
                await asyncio.sleep(0.02)
            if not acquired:
                raise ServiceUnavailableException("Failed to update the record")

            raw = await client.get(key)
            if not raw:
                raise TransferNotFoundException("Sender session not found or expired")
            updated = mutate(json.loads(raw))
            if not isinstance(updated, dict):
                raise ServiceUnavailableException("Failed to update the record")

            ttl = await client.ttl(key)
            if ttl is None or ttl <= 0:
                raise TransferNotFoundException("Sender session not found or expired")
            await client.set(key, json.dumps(updated), ex=int(ttl))
            return updated
        finally:
            if acquired:
                await client.delete(lock_key)

    async def join_sender_session(self, code: str, receiver_pk: str) -> dict:
        session_id = await self.redis.get_sender_code(code)
        if not session_id:
            raise TransferNotFoundException("Session code not found or expired")

        ttl = await self._ttl(session_id)
        if ttl <= 0:
            raise TransferNotFoundException("Session code not found or expired")

        # The claim is the race: the first public key is stored with SET NX,
        # and a second key cannot replace it.
        claim = await self.redis.claim_sender_receiver(session_id, receiver_pk, ttl)
        if claim == "other":
            raise TransferNotReadyException("This session already has a receiver")

        def mutate(data: dict) -> dict:
            session = SenderSessionModel.model_validate(data)
            # A retry after the sender has attached must not rewind the session.
            if session.status == "attached":
                return data
            session.receiver_pk = receiver_pk
            session.status = "joined"
            return session.model_dump()

        updated = await self._edit_session(session_id, mutate)
        session = SenderSessionModel.model_validate(updated)
        return {
            "session_id": session_id,
            "status": session.status,
            "expires_in": await self._ttl(session_id),
        }

    async def get_sender_session(self, session_id: str) -> dict:
        data = await self.redis.get_sender_session(session_id)
        if not data:
            raise TransferNotFoundException("Sender session not found or expired")

        session = SenderSessionModel.model_validate(data)

        # The wrapped key never travels back to the sender: it already holds
        # the AES key, and the receiver collects it once through the session.
        return {
            "session_id": session.session_id,
            "status": session.status,
            "receiver_pk": session.receiver_pk,
            "transfer_id": session.transfer_id,
            "expires_in": await self._ttl(session_id),
        }

    async def attach_transfer(
        self,
        session_id: str,
        transfer_id: str,
        wrapped_key: str,
        mode: str = "cloud",
        sender_secret: str | None = None,
    ) -> dict:
        data = await self.redis.get_sender_session(session_id)
        if not data:
            raise TransferNotFoundException("Sender session not found or expired")

        session = SenderSessionModel.model_validate(data)
        if not secrets_match(sender_secret, session.sender_secret):
            raise UnauthorizedException()
        if session.status != "joined":
            if session.status == "waiting":
                raise TransferNotReadyException("No receiver has joined this session")
            raise TransferNotReadyException("This session is already attached")

        transfer = await self.redis.get_transfer(transfer_id)
        if not transfer:
            raise TransferNotFoundException("Transfer not found or expired")

        def mutate(current: dict) -> dict:
            fresh = SenderSessionModel.model_validate(current)
            if fresh.status != "joined":
                raise TransferNotReadyException("This session is already attached")
            fresh.transfer_id = transfer_id
            fresh.wrapped_key = wrapped_key
            fresh.mode = mode
            fresh.status = "attached"
            return fresh.model_dump()

        updated = await self._edit_session(session_id, mutate)
        fresh = SenderSessionModel.model_validate(updated)
        return {
            "session_id": session_id,
            "status": fresh.status,
            "expires_in": await self._ttl(session_id),
        }

    async def collect_key(self, session_id: str) -> dict:
        data = await self.redis.get_sender_session(session_id)
        if not data:
            raise TransferNotFoundException("Sender session not found or expired")

        session = SenderSessionModel.model_validate(data)
        if session.status != "attached" or not session.wrapped_key:
            raise TransferNotReadyException("The sender has not wrapped the key yet")

        # The code can outlive a direct attempt. Only verified cloud completion
        # overrides its original hint; "uploading" also describes live P2P.
        mode = session.mode
        if mode == "p2p":
            transfer = await self.redis.get_transfer(session.transfer_id)
            if transfer and transfer.get("status") == "ready":
                mode = "cloud"

        wrapped_key = await self.redis.take_sender_wrapped_key(session_id)
        if not wrapped_key:
            raise TransferNotReadyException("The sender has not wrapped the key yet")

        return {
            "transfer_id": session.transfer_id,
            "wrapped_key": wrapped_key,
            "mode": mode,
            "expires_in": await self._ttl(session_id),
        }
