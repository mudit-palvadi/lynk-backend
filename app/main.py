import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.transfers import router as transfers_router
from app.api.receiver import router as receiver_router
from app.api.signaling import router as signaling_router
from app.core.exceptions import register_exception_handlers
from app.core.config import settings
from app.services.redis_service import redis_service


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
    yield
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
app.include_router(signaling_router, prefix="/ws", tags=["signaling"])


@app.get("/")
async def root():
    return {"message": "Welcome to Lynk API"}
