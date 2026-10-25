import secrets
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.exceptions import (
    ServiceUnavailableException,
    TransferNotFoundException,
    TransferNotReadyException,
)
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

        for _ in range(self.MAX_CODE_ATTEMPTS):
            code = self._generate_code()
            # The code lives exactly as long as the session it names, and the
            # claim is atomic so two senders never share a code.
            if not await self.redis.claim_sender_code(code, session_id, ttl):
                continue

            session = SenderSessionModel(
                session_id=session_id,
                code=code,
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
            }

        raise ServiceUnavailableException("Could not allocate a session code")

    async def _ttl(self, session_id: str) -> int:
        client = await self.redis.get_client()
        key = self.redis._get_sender_key(session_id)
        ttl = await client.ttl(key)
        return max(0, ttl) if ttl != -2 else 0

    async def join_sender_session(self, code: str, receiver_pk: str) -> dict:
        session_id = await self.redis.get_sender_code(code)
        if not session_id:
            raise TransferNotFoundException("Session code not found or expired")

        data = await self.redis.get_sender_session(session_id)
        if not data:
            raise TransferNotFoundException("Session code not found or expired")

        session = SenderSessionModel.model_validate(data)

        # A code names the session, it does not own it: the first receiver that
        # presents a key claims the session, and nobody else can take it over.
        if session.receiver_pk and session.receiver_pk != receiver_pk:
            raise TransferNotReadyException("This session already has a receiver")

        session.receiver_pk = receiver_pk
        session.status = "joined"
        await self.redis.update_sender_session(session_id, session.model_dump())

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
    ) -> dict:
        data = await self.redis.get_sender_session(session_id)
        if not data:
            raise TransferNotFoundException("Sender session not found or expired")

        session = SenderSessionModel.model_validate(data)
        if not session.receiver_pk:
            raise TransferNotReadyException("No receiver has joined this session")

        session.transfer_id = transfer_id
        session.wrapped_key = wrapped_key
        session.mode = mode
        session.status = "attached"
        await self.redis.update_sender_session(session_id, session.model_dump())

        return {
            "session_id": session_id,
            "status": session.status,
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

        res = {
            "transfer_id": session.transfer_id,
            "wrapped_key": session.wrapped_key,
            "mode": mode,
            "expires_in": await self._ttl(session_id),
        }

        # Single-use delivery: the key is wiped once handed over, so a later
        # reader of the same session cannot obtain it again.
        session.wrapped_key = None
        await self.redis.update_sender_session(session_id, session.model_dump())

        return res
