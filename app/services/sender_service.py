import secrets
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.exceptions import ServiceUnavailableException
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
