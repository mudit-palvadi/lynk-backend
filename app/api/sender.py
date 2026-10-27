from fastapi import APIRouter, Depends, Header, status

from app.core.security import RateLimiter
from app.schemas.sender import (
    SenderSessionAttachRequest,
    SenderSessionAttachResponse,
    SenderSessionCreateResponse,
    SenderSessionJoinRequest,
    SenderSessionJoinResponse,
    SenderSessionKeyResponse,
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
        Depends(RateLimiter(limit=10, window_seconds=60, bucket="sender-session-join")),
        Depends(
            RateLimiter(
                limit=100,
                window_seconds=60,
                bucket="sender-session-join-global",
                scope="global",
            )
        ),
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


@router.post("/{session_id}/attach", response_model=SenderSessionAttachResponse)
async def attach_transfer(
    session_id: str,
    payload: SenderSessionAttachRequest,
    x_lynk_sender_secret: str | None = Header(default=None),
    service: SenderService = Depends(get_sender_service),
):
    return await service.attach_transfer(
        session_id,
        payload.transfer_id,
        payload.wrapped_key,
        payload.mode,
        sender_secret=x_lynk_sender_secret,
    )


@router.get(
    "/{session_id}/key",
    response_model=SenderSessionKeyResponse,
    dependencies=[
        Depends(RateLimiter(limit=20, window_seconds=60, bucket="sender-session-key"))
    ],
)
async def collect_key(
    session_id: str,
    service: SenderService = Depends(get_sender_service),
):
    return await service.collect_key(session_id)
