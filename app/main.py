import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.transfers import router as transfers_router
from app.api.receiver import router as receiver_router
from app.api.sender import router as sender_router
from app.api.signaling import router as signaling_router
from app.api.signaling import (
    set_access_validator,
    set_session_validator,
    start_idle_sweeper,
    stop_idle_sweeper,
)
from app.core.exceptions import register_exception_handlers
from app.core.config import settings
from app.core.security import secrets_match
from app.services.redis_service import redis_service


async def _signaling_session_exists(session_id: str) -> bool:
    """Whether a signaling room id belongs to a live session.

    The room id is either a transfer id (the normal P2P flow) or a
    receiver-session id (the receiver-QR pairing flow), so both id spaces
    count as valid. Unknown ids are refused, so a room cannot be opened for
    an id that was never issued (POTENTIAL_ISSUES #28).
    """
    if await redis_service.get_transfer(session_id) is not None:
        return True
    return await redis_service.get_receiver_session(session_id) is not None


async def _signaling_access_valid(session_id: str, token: str) -> bool:
    """Whether a token authorises joining the room for ``session_id``.

    A transfer room is authorised by the transfer's access token - the same
    secret that rides in the share-link fragment and in the Flow B wrap. A
    receiver-session room belongs to the pairing flow: its id is only ever
    shown as a QR to the device being paired and the record carries no token,
    so those rooms stay id-only on purpose (POTENTIAL_ISSUES #32).
    """
    transfer = await redis_service.get_transfer(session_id)
    if transfer is not None:
        return secrets_match(token, transfer.get("access_token"))
    return await redis_service.get_receiver_session(session_id) is not None



@asynccontextmanager
async def lifespan(app: FastAPI):
    # WebRTC signaling rooms are process-local (see app/api/signaling.py).
    # Multiple workers would silently split sender/receiver into separate
    # rooms, so scale vertically until Redis-backed fan-out lands (Phase 5).
    concurrency = os.getenv("WEB_CONCURRENCY", "1")
    if concurrency.isdigit() and int(concurrency) > 1:
        logging.getLogger("lynk.startup").critical(
            "WEB_CONCURRENCY=%s but P2P signaling requires a single worker. "
            "Signaling WILL break across workers. Set WEB_CONCURRENCY=1.",
            concurrency,
        )
    redis_service.init_pool()

    # Refuse signaling rooms for ids that do not exist (#28) and evict rooms
    # that stayed idle, so a stale room cannot live for the whole process (#2).
    if settings.SIGNALING_VALIDATE_SESSION:
        set_session_validator(_signaling_session_exists)
    else:
        set_session_validator(None)
    # A room is not enough: the socket must also present the transfer's access
    # token (#32). Off by default until every client sends the auth frame.
    if settings.SIGNALING_REQUIRE_ACCESS:
        set_access_validator(_signaling_access_valid)
    else:
        set_access_validator(None)
    start_idle_sweeper(
        interval_seconds=settings.SIGNALING_SWEEPER_INTERVAL_SECONDS,
        max_idle_seconds=settings.SIGNALING_IDLE_EVICTION_SECONDS,
    )

    try:
        yield
    finally:
        stop_idle_sweeper()
        set_session_validator(None)
        set_access_validator(None)
        await redis_service.close_pool()


app = FastAPI(
    title="Lynk API",
    description="Cross-device file transfer backend",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=[
        "Content-Type",
        "Accept",
        "X-Lynk-Sender-Secret",
        "X-Lynk-Access-Token",
    ],
)

register_exception_handlers(app)

app.include_router(transfers_router, prefix="/api/v1")
app.include_router(receiver_router, prefix="/api/v1")
app.include_router(sender_router, prefix="/api/v1")
app.include_router(signaling_router, prefix="/ws", tags=["signaling"])


@app.get("/")
async def root():
    return {"message": "Welcome to Lynk API"}
