"""Security and limits regression tests.

Encodes the documented guarantees:
- Zero-knowledge: AES key NEVER reaches or persists in the backend.
- Per-file ceiling = 50 MB (`MAX_INDIVIDUAL_FILE_SIZE`, docs/PRD.md section 7).
- Default transfer session lifetime = 10 min, extendable to 30 min.
- CORS restricted to explicit allowed origins (no wildcard + credentials).
"""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.api.receiver import get_receiver_service
from app.api.transfers import get_transfer_service
from app.core.config import settings
from app.services.receiver_service import ReceiverService
from app.services.transfer_service import TransferService


@pytest.fixture()
def mock_env(monkeypatch):
    """Isolated async-mock redis/r2 wired into fresh service instances."""
    mock_redis = AsyncMock()
    mock_redis.incr = AsyncMock(return_value=1)
    mock_r2 = AsyncMock()

    receiver_service = ReceiverService(redis_service=mock_redis)
    transfer_service = TransferService(redis_service=mock_redis, r2_service=mock_r2)

    original_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_receiver_service] = lambda: receiver_service
    app.dependency_overrides[get_transfer_service] = lambda: transfer_service

    yield {"redis": mock_redis, "r2": mock_r2}

    app.dependency_overrides.clear()
    app.dependency_overrides.update(original_overrides)


client = TestClient(app)


# ---------------------------------------------------------------------------
# Configuration alignment with docs
# ---------------------------------------------------------------------------


def test_cloud_individual_file_cap_is_50mb():
    # Cloud/R2 tiers (docs decision): free = 20 MB, rewarded-ad unlock = 50 MB.
    # The server enforces the absolute ceiling: 50 MB.
    assert settings.MAX_INDIVIDUAL_FILE_SIZE == 52428800


def test_presigned_upload_url_signs_content_length(monkeypatch):
    import asyncio
    from unittest.mock import MagicMock

    from app.services.r2_service import R2Service

    mock_client = MagicMock()
    mock_client.generate_presigned_url.return_value = "https://r2.example/put"
    monkeypatch.setattr("app.services.r2_service.boto3.client", lambda *a, **k: mock_client)

    svc = R2Service()
    url = asyncio.run(svc.generate_upload_url("tid", "fid", "application/octet-stream", 4096))

    assert url == "https://r2.example/put"
    params = mock_client.generate_presigned_url.call_args.kwargs["Params"]
    assert params["ContentLength"] == 4096
    assert params["ContentType"] == "application/octet-stream"
    assert params["Key"] == "transfers/tid/fid"


def test_default_transfer_lifetime_is_10_minutes():
    assert settings.TRANSFER_LIFETIME_SECONDS == 600


def test_extended_transfer_lifetime_is_30_minutes():
    assert hasattr(settings, "EXTENDED_TRANSFER_LIFETIME_SECONDS")
    assert settings.EXTENDED_TRANSFER_LIFETIME_SECONDS == 1800


# ---------------------------------------------------------------------------
# Zero-knowledge guarantee
# ---------------------------------------------------------------------------


def test_receiver_session_response_never_contains_aes_key(mock_env):
    # Even if a LEGACY session record still carries an aes_key in Redis,
    # the API must never return it.
    session_data = {
        "session_id": "sess_123",
        "status": "attached",
        "transfer_id": "tx_123",
        "aes_key": "legacy_stale_secret_key",
        "created_at": "2026-07-15T00:00:00Z",
        "expires_at": "2026-07-15T00:10:00Z",
    }
    mock_env["redis"].get_receiver_session = AsyncMock(return_value=session_data)

    mock_client = AsyncMock()
    mock_client.ttl = AsyncMock(return_value=600)
    mock_env["redis"].get_client = AsyncMock(return_value=mock_client)

    response = client.get("/api/v1/receiver-sessions/sess_123")
    assert response.status_code == 200

    data = response.json()
    assert "aes_key" not in data


def test_attach_transfer_never_stores_client_supplied_aes_key(mock_env):
    # Legacy clients may still POST aes_key in the body. The backend must
    # accept the request (backward compatible) but MUST NOT persist the key.
    session_data = {
        "session_id": "sess_123",
        "status": "waiting",
        "transfer_id": None,
        "created_at": "2026-07-15T00:00:00Z",
        "expires_at": "2026-07-15T00:10:00Z",
    }
    transfer_data = {
        "transfer_id": "tx_123",
        "status": "ready",
        "created_at": "2026-07-15T00:00:00Z",
        "expires_at": "2026-07-15T00:30:00Z",
        "total_files": 1,
        "total_size": 100,
        "files": [],
    }
    mock_env["redis"].get_receiver_session = AsyncMock(return_value=session_data)
    mock_env["redis"].get_transfer = AsyncMock(return_value=transfer_data)
    mock_env["redis"].update_receiver_session = AsyncMock()

    payload = {"transfer_id": "tx_123", "aes_key": "attacker_or_legacy_key"}
    response = client.post("/api/v1/receiver-sessions/sess_123/attach-transfer", json=payload)
    assert response.status_code == 200

    mock_env["redis"].update_receiver_session.assert_called_once()
    persisted = mock_env["redis"].update_receiver_session.call_args.args[1]
    assert "aes_key" not in persisted
    assert "attacker_or_legacy_key" not in str(persisted)


def test_attach_transfer_stores_wrapped_key(mock_env):
    # ECDH key-wrap design: sender wraps the AES key to the receiver's
    # ephemeral public key client-side. The backend relays the opaque
    # ciphertext blob only.
    session_data = {
        "session_id": "sess_123",
        "status": "waiting",
        "transfer_id": None,
        "created_at": "2026-07-15T00:00:00Z",
        "expires_at": "2026-07-15T00:10:00Z",
    }
    transfer_data = {
        "transfer_id": "tx_123",
        "status": "ready",
        "created_at": "2026-07-15T00:00:00Z",
        "expires_at": "2026-07-15T00:30:00Z",
        "total_files": 1,
        "total_size": 100,
        "files": [],
    }
    mock_env["redis"].get_receiver_session = AsyncMock(return_value=session_data)
    mock_env["redis"].get_transfer = AsyncMock(return_value=transfer_data)
    mock_env["redis"].update_receiver_session = AsyncMock()

    wrapped = "x25519-sealed-blob-base64=="
    response = client.post(
        "/api/v1/receiver-sessions/sess_123/attach-transfer",
        json={"transfer_id": "tx_123", "wrapped_key": wrapped},
    )
    assert response.status_code == 200

    mock_env["redis"].update_receiver_session.assert_called_once()
    persisted = mock_env["redis"].update_receiver_session.call_args.args[1]
    assert persisted["wrapped_key"] == wrapped


def test_get_receiver_session_delivers_wrapped_key_once(mock_env):
    # Single-use delivery: first GET returns the wrapped_key and wipes it
    # from Redis so a later reader (or attacker with the session id) gets nothing.
    session_data = {
        "session_id": "sess_123",
        "status": "attached",
        "transfer_id": "tx_123",
        "wrapped_key": "x25519-sealed-blob-base64==",
        "created_at": "2026-07-15T00:00:00Z",
        "expires_at": "2026-07-15T00:10:00Z",
    }
    mock_env["redis"].get_receiver_session = AsyncMock(return_value=session_data)

    mock_client = AsyncMock()
    mock_client.ttl = AsyncMock(return_value=600)
    mock_env["redis"].get_client = AsyncMock(return_value=mock_client)

    response = client.get("/api/v1/receiver-sessions/sess_123")
    assert response.status_code == 200
    assert response.json()["wrapped_key"] == "x25519-sealed-blob-base64=="

    mock_env["redis"].update_receiver_session.assert_called_once()
    wiped = mock_env["redis"].update_receiver_session.call_args.args[1]
    assert wiped["wrapped_key"] is None


# ---------------------------------------------------------------------------
# Session extension (Rewarded Ad unlock: 10 min -> 30 min)
# ---------------------------------------------------------------------------


def test_extend_transfer_returns_extended_expiry(mock_env):
    transfer_data = {
        "transfer_id": "tx_123",
        "status": "ready",
        "created_at": "2026-07-15T00:00:00Z",
        "expires_at": "2026-07-15T00:10:00Z",
        "total_files": 1,
        "total_size": 100,
        "files": [],
    }
    mock_env["redis"].get_transfer = AsyncMock(return_value=transfer_data)
    mock_env["redis"].extend_transfer = AsyncMock()
    mock_env["redis"].get_client = AsyncMock(return_value=AsyncMock())

    response = client.post("/api/v1/transfers/tx_123/extend")
    assert response.status_code == 200

    data = response.json()
    assert data["transfer_id"] == "tx_123"
    assert data["expires_in"] == settings.EXTENDED_TRANSFER_LIFETIME_SECONDS

    mock_env["redis"].extend_transfer.assert_called_once()


def test_extend_transfer_not_found(mock_env):
    mock_env["redis"].get_transfer = AsyncMock(return_value=None)
    response = client.post("/api/v1/transfers/tx_missing/extend")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# CORS hardening
# ---------------------------------------------------------------------------


def _preflight(origin: str):
    return client.options(
        "/api/v1/transfers",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )


def test_cors_unknown_origin_is_not_granted_access():
    response = _preflight("https://malicious.example.com")
    assert "access-control-allow-origin" not in response.headers


def test_cors_wildcard_origin_is_not_used():
    response = _preflight("https://some-random-site.org")
    acao = response.headers.get("access-control-allow-origin")
    assert acao != "*"


def test_cors_allowed_origin_is_echoed():
    allowed = settings.ALLOWED_ORIGINS[0]
    response = _preflight(allowed)
    assert response.headers.get("access-control-allow-origin") == allowed


def test_cors_production_domain_lynkshare_app_is_allowed():
    # The canonical production deep-link domain must be in the default
    # allowlist (docs: domain purchased via Name.com, proxied by Cloudflare).
    assert "https://lynkshare.app" in settings.ALLOWED_ORIGINS
    assert "https://lynk.app" not in settings.ALLOWED_ORIGINS

    response = _preflight("https://lynkshare.app")
    assert response.headers.get("access-control-allow-origin") == ("https://lynkshare.app")


def test_cors_does_not_allow_credentials_with_wildcard():
    # The dangerous combination (origins=["*"], credentials=True) must be gone.
    cors_middleware = None
    for mw in app.user_middleware:
        if mw.cls.__name__ == "CORSMiddleware":
            cors_middleware = mw
            break
    assert cors_middleware is not None
    kwargs = cors_middleware.kwargs
    assert kwargs.get("allow_origins") != ["*"]
    assert kwargs.get("allow_credentials") is False
