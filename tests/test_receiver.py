from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.api.receiver import get_receiver_service
from app.api.transfers import get_transfer_service
from app.services.receiver_service import ReceiverService
from app.services.transfer_service import TransferService

from app.services.redis_service import redis_service

mock_redis = AsyncMock()
mock_redis.incr = AsyncMock(return_value=1)
redis_service.get_client = AsyncMock(return_value=mock_redis)
mock_r2 = AsyncMock()

mock_receiver_service = ReceiverService(redis_service=mock_redis)
mock_transfer_service = TransferService(redis_service=mock_redis, r2_service=mock_r2)


def override_get_receiver_service():
    return mock_receiver_service


def override_get_transfer_service():
    return mock_transfer_service


app.dependency_overrides[get_receiver_service] = override_get_receiver_service
app.dependency_overrides[get_transfer_service] = override_get_transfer_service

client = TestClient(app)


@pytest.fixture(autouse=True)
def run_around_tests():
    mock_redis.reset_mock()
    mock_redis.incr = AsyncMock(return_value=1)
    mock_redis.get_client = AsyncMock(return_value=mock_redis)
    mock_r2.reset_mock()
    yield


def test_create_receiver_session_success():
    mock_redis.set_receiver_session = AsyncMock()

    response = client.post("/api/v1/receiver-sessions")
    assert response.status_code == 201

    data = response.json()
    assert "session_id" in data
    assert data["status"] == "waiting"
    mock_redis.set_receiver_session.assert_called_once()


def test_get_receiver_session_success():
    session_data = {
        "session_id": "sess_123",
        "status": "waiting",
        "transfer_id": None,
        "aes_key": None,
        "created_at": "2026-07-15T00:00:00Z",
        "expires_at": "2026-07-15T00:10:00Z",
    }
    mock_redis.get_receiver_session = AsyncMock(return_value=session_data)

    mock_client = AsyncMock()
    mock_client.ttl = AsyncMock(side_effect=lambda key: 600)
    mock_redis.get_client = AsyncMock(return_value=mock_client)

    response = client.get("/api/v1/receiver-sessions/sess_123")
    assert response.status_code == 200

    data = response.json()
    assert data["session_id"] == "sess_123"
    assert data["status"] == "waiting"
    assert data["transfer_id"] is None
    assert "aes_key" not in data
    assert data["expires_in"] == 600


def test_get_receiver_session_attached_has_no_aes_key():
    session_data = {
        "session_id": "sess_123",
        "status": "attached",
        "transfer_id": "tx_123",
        "aes_key": None,
        "created_at": "2026-07-15T00:00:00Z",
        "expires_at": "2026-07-15T00:10:00Z",
    }
    mock_redis.get_receiver_session = AsyncMock(return_value=session_data)

    mock_client = AsyncMock()
    mock_client.ttl = AsyncMock(side_effect=lambda key: 600)
    mock_redis.get_client = AsyncMock(return_value=mock_client)

    response = client.get("/api/v1/receiver-sessions/sess_123")
    assert response.status_code == 200

    data = response.json()
    assert data["session_id"] == "sess_123"
    assert data["status"] == "attached"
    assert data["transfer_id"] == "tx_123"
    assert "aes_key" not in data


def test_get_receiver_session_not_found():
    mock_redis.get_receiver_session = AsyncMock(return_value=None)
    response = client.get("/api/v1/receiver-sessions/sess_missing")
    assert response.status_code == 404


def test_attach_transfer_success_without_aes_key():
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
    mock_redis.get_transfer = AsyncMock(return_value=transfer_data)

    async def mutate_json(key, mutate, ttl_seconds=None):
        return mutate(dict(session_data))

    mock_redis.mutate_json = mutate_json

    payload = {"transfer_id": "tx_123"}
    response = client.post("/api/v1/receiver-sessions/sess_123/attach-transfer", json=payload)

    assert response.status_code == 200
    assert response.json()["detail"] == "Transfer attached successfully"


def test_attach_transfer_session_not_found():
    mock_redis.get_transfer = AsyncMock(return_value={"transfer_id": "tx_123"})
    mock_redis.mutate_json = AsyncMock(return_value=None)
    payload = {"transfer_id": "tx_123"}
    response = client.post("/api/v1/receiver-sessions/sess_missing/attach-transfer", json=payload)
    assert response.status_code == 404


def test_attach_transfer_transfer_not_found():
    session_data = {
        "session_id": "sess_123",
        "status": "waiting",
        "transfer_id": None,
        "created_at": "2026-07-15T00:00:00Z",
        "expires_at": "2026-07-15T00:10:00Z",
    }
    mock_redis.get_receiver_session = AsyncMock(return_value=session_data)
    mock_redis.get_transfer = AsyncMock(return_value=None)

    payload = {"transfer_id": "tx_missing"}
    response = client.post("/api/v1/receiver-sessions/sess_123/attach-transfer", json=payload)
    assert response.status_code == 404


def test_cancel_receiver_session():
    mock_redis.delete_receiver_session = AsyncMock()
    response = client.delete("/api/v1/receiver-sessions/sess_123")
    assert response.status_code == 204
    mock_redis.delete_receiver_session.assert_called_once_with("sess_123")
