from fastapi import APIRouter, Depends, Header, Request, status
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

from app.core.security import RateLimiter, resolve_client_ip
from app.core.config import settings
from app.core.exceptions import TransferNotFoundException
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
    request: Request,
    payload: TransferCreate,
    service: TransferService = Depends(get_transfer_service),
):
    files = [f.model_dump() for f in payload.files]
    return await service.create_transfer(files, client_ip=resolve_client_ip(request))


@router.post("/{transfer_id}/files/{file_id}/complete", response_model=FileCompleteResponse)
async def complete_file(
    transfer_id: str,
    file_id: str,
    service: TransferService = Depends(get_transfer_service),
    x_lynk_sender_secret: str | None = Header(default=None),
):
    return await service.complete_file(
        transfer_id, file_id, sender_secret=x_lynk_sender_secret
    )


@router.get("/{transfer_id}", response_model=TransferMetadataResponse)
async def get_transfer(
    transfer_id: str,
    service: TransferService = Depends(get_transfer_service),
    x_lynk_access_token: str | None = Header(default=None),
):
    return await service.get_transfer_metadata(transfer_id, access_token=x_lynk_access_token)


@router.post("/{transfer_id}/downloads", response_model=TransferDownloadResponse)
async def get_download_urls(
    transfer_id: str,
    payload: FileDownloadRequest,
    service: TransferService = Depends(get_transfer_service),
    x_lynk_access_token: str | None = Header(default=None),
):
    return await service.get_download_urls(
        transfer_id, payload.file_ids, access_token=x_lynk_access_token
    )


@router.post(
    "/{transfer_id}/extend",
    response_model=TransferExtendResponse,
    dependencies=[Depends(RateLimiter(limit=5, window_seconds=60, bucket="transfer-extend"))],
)
async def extend_transfer(
    transfer_id: str,
    service: TransferService = Depends(get_transfer_service),
    x_lynk_sender_secret: str | None = Header(default=None),
):
    return await service.extend_transfer(transfer_id, sender_secret=x_lynk_sender_secret)


@router.delete("/{transfer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_transfer(
    transfer_id: str,
    service: TransferService = Depends(get_transfer_service),
    x_lynk_sender_secret: str | None = Header(default=None),
):
    await service.cancel_transfer(transfer_id, sender_secret=x_lynk_sender_secret)


@router.get(
    "/{transfer_id}/turn-credentials",
    dependencies=[
        Depends(RateLimiter(limit=10, window_seconds=60, bucket="transfer-turn-credentials"))
    ],
)
async def get_turn_credentials(
    transfer_id: str,
    service: TransferService = Depends(get_transfer_service),
):
    """Short-lived TURN relay credentials for the P2P ICE ladder.

    Returns `enabled: false` when TURN is not configured so clients degrade
    gracefully to STUN-only + R2 fallback.

    Credentials are only minted for a transfer that actually exists
    (POTENTIAL_ISSUES #25): otherwise anybody could ask the relay for
    credentials for an id they made up.
    """
    if not settings.TURN_URLS or not settings.TURN_SECRET:
        return {"enabled": False, "urls": [], "username": "", "credential": "", "ttl": 0}
    if not await service.transfer_exists(transfer_id):
        raise TransferNotFoundException()
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
