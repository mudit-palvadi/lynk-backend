from fastapi import APIRouter, Depends, status

from app.core.security import RateLimiter
from app.schemas.sender import SenderSessionCreateResponse
from app.services.redis_service import redis_service
from app.services.sender_service import SenderService

router = APIRouter(prefix="/sender-sessions", tags=["sender-sessions"])


def get_sender_service() -> SenderService:
    return SenderService(redis_service=redis_service)


@router.post(
    "",
    response_model=SenderSessionCreateResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(RateLimiter(limit=10, window_seconds=60, bucket="sender-session-create"))
    ],
)
async def create_sender_session(service: SenderService = Depends(get_sender_service)):
    return await service.create_sender_session()
