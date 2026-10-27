import secrets
from datetime import datetime, timedelta, timezone
from app.core.config import settings
from app.core.exceptions import TransferNotFoundException
from app.models.receiver import ReceiverSessionModel
from app.services.redis_service import RedisService


class ReceiverService:
    def __init__(self, redis_service: RedisService) -> None:
        self.redis = redis_service

    def _generate_id(self, length: int = 12) -> str:
        return secrets.token_urlsafe(length)[:length]

    async def create_receiver_session(self) -> dict:
        session_id = self._generate_id()
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=settings.RECEIVER_SESSION_LIFETIME_SECONDS)

        session = ReceiverSessionModel(
            session_id=session_id,
            status="waiting",
            created_at=now.isoformat(),
            expires_at=expires_at.isoformat(),
        )

        await self.redis.set_receiver_session(
            session_id, session.model_dump(), settings.RECEIVER_SESSION_LIFETIME_SECONDS
        )

        return {
            "session_id": session_id,
            "status": "waiting",
            "expires_in": settings.RECEIVER_SESSION_LIFETIME_SECONDS,
        }

    async def get_receiver_session(self, session_id: str) -> dict:
        data = await self.redis.get_receiver_session(session_id)
        if not data:
            raise TransferNotFoundException("Receiver session not found or expired")

        session = ReceiverSessionModel.model_validate(data)

        client = await self.redis.get_client()
        key = self.redis._get_receiver_key(session_id)
        ttl = await client.ttl(key)
        expires_in = max(0, ttl) if ttl != -2 else 0

        # The key is taken under the record lock. A second reader, even one
        # that loaded this same snapshot, receives nothing.
        wrapped_key = None
        if session.status == "attached" and session.wrapped_key:
            wrapped_key = await self.redis.take_receiver_wrapped_key(session_id)

        return {
            "session_id": session.session_id,
            "status": session.status,
            "transfer_id": session.transfer_id,
            "wrapped_key": wrapped_key,
            "expires_in": expires_in,
        }

    async def attach_transfer(
        self, session_id: str, transfer_id: str, wrapped_key: str | None = None
    ) -> None:
        session_data = await self.redis.get_receiver_session(session_id)
        if not session_data:
            raise TransferNotFoundException("Receiver session not found or expired")

        transfer_data = await self.redis.get_transfer(transfer_id)
        if not transfer_data:
            raise TransferNotFoundException("Transfer not found or expired")

        session = ReceiverSessionModel.model_validate(session_data)
        session.status = "attached"
        session.transfer_id = transfer_id
        if wrapped_key:
            session.wrapped_key = wrapped_key

        await self.redis.update_receiver_session(session_id, session.model_dump())

    async def cancel_receiver_session(self, session_id: str) -> None:
        await self.redis.delete_receiver_session(session_id)
