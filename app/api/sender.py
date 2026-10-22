from fastapi import APIRouter, Depends, status

from app.core.security import RateLimiter
from app.schemas.sender import (
    SenderSessionCreateResponse,
    SenderSessionJoinRequest,
    SenderSessionJoinResponse,
    SenderSessionResponse,
)
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


@router.post(
    "/join",
    response_model=SenderSessionJoinResponse,
    dependencies=[
        Depends(RateLimiter(limit=10, window_seconds=60, bucket="sender-session-join"))
    ],
)
async def join_sender_session(
    payload: SenderSessionJoinRequest,
    service: SenderService = Depends(get_sender_service),
):
    return await service.join_sender_session(payload.code, payload.receiver_pk)


@router.get("/{session_id}", response_model=SenderSessionResponse)
async def get_sender_session(
    session_id: str,
    service: SenderService = Depends(get_sender_service),
):
    return await service.get_sender_session(session_id)
