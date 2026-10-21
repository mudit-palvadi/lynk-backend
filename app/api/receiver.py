from fastapi import APIRouter, Depends, status
from app.schemas.receiver import (
    ReceiverSessionCreateResponse,
    ReceiverSessionResponse,
    AttachTransferRequest,
)
from app.services.redis_service import redis_service
from app.services.receiver_service import ReceiverService

from app.core.security import RateLimiter

router = APIRouter(prefix="/receiver-sessions", tags=["receiver-sessions"])


def get_receiver_service() -> ReceiverService:
    return ReceiverService(redis_service=redis_service)


@router.post(
    "",
    response_model=ReceiverSessionCreateResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(RateLimiter(limit=5, window_seconds=60, bucket="receiver-session-create"))
    ],
)
async def create_receiver_session(service: ReceiverService = Depends(get_receiver_service)):
    return await service.create_receiver_session()


@router.get("/{session_id}", response_model=ReceiverSessionResponse)
async def get_receiver_session(
    session_id: str, service: ReceiverService = Depends(get_receiver_service)
):
    return await service.get_receiver_session(session_id)


@router.post("/{session_id}/attach-transfer", status_code=status.HTTP_200_OK)
async def attach_transfer(
    session_id: str,
    payload: AttachTransferRequest,
    service: ReceiverService = Depends(get_receiver_service),
):
    await service.attach_transfer(session_id, payload.transfer_id, payload.wrapped_key)
    return {"detail": "Transfer attached successfully"}


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_receiver_session(
    session_id: str, service: ReceiverService = Depends(get_receiver_service)
):
    await service.cancel_receiver_session(session_id)
