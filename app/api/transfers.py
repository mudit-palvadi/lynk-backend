from fastapi import APIRouter, Depends, status
from app.schemas.transfer import (
    TransferCreate,
    TransferCreateResponse,
    TransferMetadataResponse,
    FileCompleteResponse,
    FileDownloadRequest,
    TransferDownloadResponse,
    TransferExtendResponse,
)
from app.services.redis_service import redis_service
from app.services.r2_service import r2_service
from app.services.transfer_service import TransferService

from app.core.security import RateLimiter
from app.core.config import settings
from app.services.turn_service import generate_turn_credentials

router = APIRouter(prefix="/transfers", tags=["transfers"])


def get_transfer_service() -> TransferService:
    return TransferService(redis_service=redis_service, r2_service=r2_service)


@router.post(
    "",
    response_model=TransferCreateResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(RateLimiter(limit=5, window_seconds=60, bucket="transfer-create"))],
)
async def create_transfer(
    payload: TransferCreate, service: TransferService = Depends(get_transfer_service)
):
    files = [f.model_dump() for f in payload.files]
    return await service.create_transfer(files)


@router.post("/{transfer_id}/files/{file_id}/complete", response_model=FileCompleteResponse)
async def complete_file(
    transfer_id: str, file_id: str, service: TransferService = Depends(get_transfer_service)
):
    return await service.complete_file(transfer_id, file_id)


@router.get("/{transfer_id}", response_model=TransferMetadataResponse)
async def get_transfer(transfer_id: str, service: TransferService = Depends(get_transfer_service)):
    return await service.get_transfer_metadata(transfer_id)


@router.post("/{transfer_id}/downloads", response_model=TransferDownloadResponse)
async def get_download_urls(
    transfer_id: str,
    payload: FileDownloadRequest,
    service: TransferService = Depends(get_transfer_service),
):
    return await service.get_download_urls(transfer_id, payload.file_ids)


@router.post(
    "/{transfer_id}/extend",
    response_model=TransferExtendResponse,
    dependencies=[Depends(RateLimiter(limit=5, window_seconds=60, bucket="transfer-extend"))],
)
async def extend_transfer(
    transfer_id: str, service: TransferService = Depends(get_transfer_service)
):
    return await service.extend_transfer(transfer_id)


@router.delete("/{transfer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_transfer(
    transfer_id: str, service: TransferService = Depends(get_transfer_service)
):
    await service.cancel_transfer(transfer_id)


@router.get(
    "/{transfer_id}/turn-credentials",
    dependencies=[Depends(RateLimiter(limit=10, window_seconds=60, bucket="transfer-turn-credentials"))],
)
async def get_turn_credentials(transfer_id: str):
    """Short-lived TURN relay credentials for the P2P ICE ladder.

    Returns `enabled: false` when TURN is not configured so clients degrade
    gracefully to STUN-only + R2 fallback.
    """
    if not settings.TURN_URLS or not settings.TURN_SECRET:
        return {"enabled": False, "urls": [], "username": "", "credential": "", "ttl": 0}
    creds = generate_turn_credentials(
        secret=settings.TURN_SECRET,
        session_id=transfer_id,
        ttl_seconds=settings.TURN_CREDENTIAL_TTL_SECONDS,
    )
    return {
        "enabled": True,
        "urls": [u.strip() for u in settings.TURN_URLS.split(",") if u.strip()],
        **creds,
    }
