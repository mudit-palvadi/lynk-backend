import json
from unittest.mock import ANY, AsyncMock
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.api.transfers import get_transfer_service
from app.services.transfer_service import TransferService

from app.services.redis_service import redis_service

mock_redis = AsyncMock()
mock_redis.incr = AsyncMock(return_value=1)
mock_redis.eval = AsyncMock(return_value=[1, 60000])
mock_redis.get_client = AsyncMock(return_value=mock_redis)
mock_redis.zrange = AsyncMock(return_value=[])
redis_service.get_client = AsyncMock(return_value=mock_redis)
mock_r2 = AsyncMock()

mock_transfer_service = TransferService(redis_service=mock_redis, r2_service=mock_r2)


def override_get_transfer_service():
    return mock_transfer_service


app.dependency_overrides[get_transfer_service] = override_get_transfer_service

client = TestClient(app)


@pytest.fixture(autouse=True)
def run_around_tests():
    mock_redis.reset_mock()
    mock_redis.incr = AsyncMock(return_value=1)
    mock_redis.eval = AsyncMock(return_value=[1, 60000])
    mock_redis.get_client = AsyncMock(return_value=mock_redis)
    mock_redis.zrange = AsyncMock(return_value=[])
    mock_r2.reset_mock()
    yield


def test_create_transfer_success():
    mock_redis.set_transfer = AsyncMock()
    mock_r2.generate_upload_url = AsyncMock(return_value="https://r2.mock/upload")

    payload = {
        "files": [
            {"file_name": "photo.jpg", "file_size": 1024 * 1024 * 10, "content_type": "image/jpeg"},
            {"file_name": "video.mp4", "file_size": 1024 * 1024 * 20, "content_type": "video/mp4"},
        ]
    }

    response = client.post("/api/v1/transfers", json=payload)
    assert response.status_code == 201

    data = response.json()
    assert "transfer_id" in data
    assert data["status"] == "uploading"
    assert len(data["files"]) == 2
    assert data["files"][0]["upload_url"] == "https://r2.mock/upload"
    assert data["sender_secret"]
    assert data["access_token"]
    assert data["sender_secret"] != data["access_token"]
    stored = mock_redis.set_transfer.call_args.args[1]
    assert stored["sender_secret"] == data["sender_secret"]
    assert stored["access_token"] == data["access_token"]
    mock_redis.set_transfer.assert_called_once()
    mock_r2.generate_upload_url.assert_any_call(ANY, ANY, "image/jpeg", 1024 * 1024 * 10)


def test_create_transfer_limit_exceeded_file_count():
    payload = {
        "files": [
            {"file_name": f"f_{i}.txt", "file_size": 100, "content_type": "text/plain"}
            for i in range(15)
        ]
    }
    response = client.post("/api/v1/transfers", json=payload)
    assert response.status_code == 413
    assert "limit exceeded" in response.json()["detail"]


def test_create_transfer_limit_exceeded_individual_size():
    payload = {
        "files": [
            {
                "file_name": "large.zip",
                "file_size": 160 * 1024 * 1024,
                "content_type": "application/zip",
            }
        ]
    }
    response = client.post("/api/v1/transfers", json=payload)
    assert response.status_code == 413
    assert "exceeds individual size limit" in response.json()["detail"]


def test_create_transfer_allows_20mb_free_tier_file():
    mock_redis.set_transfer = AsyncMock()
    mock_r2.generate_upload_url = AsyncMock(return_value="https://r2.mock/upload")

    payload = {
        "files": [
            {
                "file_name": "free.zip",
                "file_size": 20 * 1024 * 1024,
                "content_type": "application/zip",
            }
        ]
    }
    response = client.post("/api/v1/transfers", json=payload)
    assert response.status_code == 201


def test_create_transfer_allows_50mb_unlocked_file():
    mock_redis.set_transfer = AsyncMock()
    mock_r2.generate_upload_url = AsyncMock(return_value="https://r2.mock/upload")

    payload = {
        "files": [
            {
                "file_name": "unlocked.zip",
                "file_size": 50 * 1024 * 1024,
                "content_type": "application/zip",
            }
        ]
    }
    response = client.post("/api/v1/transfers", json=payload)
    assert response.status_code == 201


def test_create_transfer_allows_two_files_that_sum_to_50mb():
    mock_redis.set_transfer = AsyncMock()
    mock_r2.generate_upload_url = AsyncMock(return_value="https://r2.mock/upload")

    payload = {
        "files": [
            {
                "file_name": "a.zip",
                "file_size": 20 * 1024 * 1024,
                "content_type": "application/zip",
            },
            {
                "file_name": "b.zip",
                "file_size": 30 * 1024 * 1024,
                "content_type": "application/zip",
            },
        ]
    }
    response = client.post("/api/v1/transfers", json=payload)
    assert response.status_code == 201


def test_create_transfer_refuses_a_total_over_50mb():
    payload = {
        "files": [
            {
                "file_name": "a.zip",
                "file_size": 30 * 1024 * 1024,
                "content_type": "application/zip",
            },
            {
                "file_name": "b.zip",
                "file_size": 30 * 1024 * 1024,
                "content_type": "application/zip",
            },
        ]
    }
    response = client.post("/api/v1/transfers", json=payload)
    assert response.status_code == 413
    assert "Total transfer size limit exceeded" in response.json()["detail"]


def test_create_transfer_limit_exceeded_total_size():
    from unittest.mock import patch

    payload = {
        "files": [
            {
                "file_name": "part1.zip",
                "file_size": 40 * 1024 * 1024,
                "content_type": "application/zip",
            },
            {
                "file_name": "part2.zip",
                "file_size": 40 * 1024 * 1024,
                "content_type": "application/zip",
            },
            {
                "file_name": "part3.zip",
                "file_size": 40 * 1024 * 1024,
                "content_type": "application/zip",
            },
        ]
    }
    with patch("app.services.transfer_service.settings") as mock_settings:
        mock_settings.MAX_FILES_PER_TRANSFER = 10
        mock_settings.MAX_INDIVIDUAL_FILE_SIZE = 50 * 1024 * 1024
        mock_settings.MAX_TOTAL_TRANSFER_SIZE = 100 * 1024 * 1024

        response = client.post("/api/v1/transfers", json=payload)
        assert response.status_code == 413
        assert "Total transfer size limit exceeded" in response.json()["detail"]


def test_complete_file_success():
    transfer_data = {
        "transfer_id": "tx_123",
        "sender_secret": "sender-secret",
        "access_token": "access-token",
        "status": "uploading",
        "created_at": "2026-07-15T00:00:00Z",
        "expires_at": "2026-07-15T00:30:00Z",
        "total_files": 2,
        "total_size": 100,
        "files": [
            {
                "file_id": "fid_1",
                "file_name": "a.txt",
                "file_size": 50,
                "content_type": "text/plain",
                "status": "pending",
            },
            {
                "file_id": "fid_2",
                "file_name": "b.txt",
                "file_size": 50,
                "content_type": "text/plain",
                "status": "uploaded",
            },
        ],
    }
    mock_redis.get_transfer = AsyncMock(return_value=transfer_data)
    mock_r2.verify_file_exists = AsyncMock(return_value=50)

    async def mutate_json(key, mutate, ttl_seconds=None):
        updated = mutate(json.loads(json.dumps(transfer_data)))
        mutate_json.saved = updated
        return updated

    mock_redis.mutate_json = mutate_json

    response = client.post(
        "/api/v1/transfers/tx_123/files/fid_1/complete",
        headers={"X-Lynk-Sender-Secret": "sender-secret"},
    )
    assert response.status_code == 200

    data = response.json()
    assert data["file_id"] == "fid_1"
    assert data["status"] == "uploaded"
    assert data["transfer_status"] == "ready"
    assert mutate_json.saved["files"][0]["status"] == "uploaded"
    assert mutate_json.saved["status"] == "ready"


def test_create_transfer_rejects_non_positive_file_size():
    mock_redis.set_transfer = AsyncMock()
    for size in (0, -1024):
        response = client.post(
            "/api/v1/transfers",
            json={
                "files": [
                    {
                        "file_name": "a.bin",
                        "file_size": size,
                        "content_type": "application/octet-stream",
                    }
                ]
            },
        )
        assert response.status_code == 422
    mock_redis.set_transfer.assert_not_called()


def test_complete_file_rejects_size_mismatch():
    transfer_data = {
        "transfer_id": "tx_123",
        "sender_secret": "sender-secret",
        "access_token": "access-token",
        "status": "uploading",
        "created_at": "2026-07-15T00:00:00Z",
        "expires_at": "2026-07-15T00:30:00Z",
        "total_files": 1,
        "total_size": 50,
        "files": [
            {
                "file_id": "fid_1",
                "file_name": "a.txt",
                "file_size": 50,
                "content_type": "text/plain",
                "status": "pending",
            }
        ],
    }
    mock_redis.get_transfer = AsyncMock(return_value=transfer_data)
    mock_r2.verify_file_exists = AsyncMock(return_value=50000000)
    mock_r2.delete_objects = AsyncMock()
    mock_redis.update_transfer = AsyncMock()

    response = client.post(
        "/api/v1/transfers/tx_123/files/fid_1/complete",
        headers={"X-Lynk-Sender-Secret": "sender-secret"},
    )
    assert response.status_code == 400
    assert "does not match" in response.json()["detail"]
    mock_redis.update_transfer.assert_not_called()
    mock_r2.delete_objects.assert_awaited_once_with("tx_123", ["fid_1"])


def test_complete_file_not_found():
    mock_redis.get_transfer = AsyncMock(return_value=None)
    response = client.post("/api/v1/transfers/tx_missing/files/fid_1/complete")
    assert response.status_code == 404


def test_get_metadata_success():
    transfer_data = {
        "transfer_id": "tx_123",
        "sender_secret": "sender-secret",
        "access_token": "access-token",
        "status": "ready",
        "created_at": "2026-07-15T00:00:00Z",
        "expires_at": "2026-07-15T00:30:00Z",
        "total_files": 1,
        "total_size": 100,
        "files": [
            {
                "file_id": "fid_1",
                "file_name": "a.txt",
                "file_size": 100,
                "content_type": "text/plain",
                "status": "uploaded",
            }
        ],
    }
    mock_redis.get_transfer = AsyncMock(return_value=transfer_data)

    mock_client = AsyncMock()
    mock_client.ttl = AsyncMock(side_effect=lambda key: 1200)
    mock_redis.get_client = AsyncMock(return_value=mock_client)

    response = client.get(
        "/api/v1/transfers/tx_123",
        headers={"X-Lynk-Access-Token": "access-token"},
    )
    assert response.status_code == 200

    data = response.json()
    assert data["transfer_id"] == "tx_123"
    assert data["status"] == "ready"
    assert data["expires_in"] == 1200
    assert len(data["files"]) == 1
    assert data["files"][0]["file_id"] == "fid_1"
    assert "sender_secret" not in data
    assert "access_token" not in data


def test_get_download_urls_not_ready():
    transfer_data = {
        "transfer_id": "tx_123",
        "sender_secret": "sender-secret",
        "access_token": "access-token",
        "status": "uploading",
        "created_at": "2026-07-15T00:00:00Z",
        "expires_at": "2026-07-15T00:30:00Z",
        "total_files": 1,
        "total_size": 100,
        "files": [
            {
                "file_id": "fid_1",
                "file_name": "a.txt",
                "file_size": 100,
                "content_type": "text/plain",
                "status": "pending",
            }
        ],
    }
    mock_redis.get_transfer = AsyncMock(return_value=transfer_data)

    response = client.post(
        "/api/v1/transfers/tx_123/downloads",
        json={},
        headers={"X-Lynk-Access-Token": "access-token"},
    )
    assert response.status_code == 409


def test_get_download_urls_success():
    transfer_data = {
        "transfer_id": "tx_123",
        "sender_secret": "sender-secret",
        "access_token": "access-token",
        "status": "ready",
        "created_at": "2026-07-15T00:00:00Z",
        "expires_at": "2026-07-15T00:30:00Z",
        "total_files": 1,
        "total_size": 100,
        "files": [
            {
                "file_id": "fid_1",
                "file_name": "a.txt",
                "file_size": 100,
                "content_type": "text/plain",
                "status": "uploaded",
            }
        ],
    }
    mock_redis.get_transfer = AsyncMock(return_value=transfer_data)
    mock_r2.generate_download_url = AsyncMock(return_value="https://r2.mock/download")

    response = client.post(
        "/api/v1/transfers/tx_123/downloads",
        json={},
        headers={"X-Lynk-Access-Token": "access-token"},
    )
    assert response.status_code == 200

    data = response.json()
    assert len(data["files"]) == 1
    assert data["files"][0]["file_id"] == "fid_1"
    assert data["files"][0]["download_url"] == "https://r2.mock/download"


def test_cancel_transfer():
    transfer_data = {
        "transfer_id": "tx_123",
        "sender_secret": "sender-secret",
        "access_token": "access-token",
        "status": "ready",
        "created_at": "2026-07-15T00:00:00Z",
        "expires_at": "2026-07-15T00:30:00Z",
        "total_files": 1,
        "total_size": 100,
        "files": [
            {
                "file_id": "fid_1",
                "file_name": "a.txt",
                "file_size": 100,
                "content_type": "text/plain",
                "status": "uploaded",
            }
        ],
    }
    mock_redis.get_transfer = AsyncMock(return_value=transfer_data)
    mock_redis.delete_transfer = AsyncMock()
    mock_r2.delete_objects = AsyncMock()

    response = client.delete(
        "/api/v1/transfers/tx_123",
        headers={"X-Lynk-Sender-Secret": "sender-secret"},
    )
    assert response.status_code == 204
    mock_redis.delete_transfer.assert_called_once_with("tx_123")
    mock_r2.delete_objects.assert_called_once_with("tx_123", ["fid_1"])


def test_create_transfer_storage_cap_exceeded():
    mock_redis.set_transfer = AsyncMock()
    mock_redis.zrange = AsyncMock(
        return_value=["tx_old:10500000000"]
    )  # 10.5 GB active size (> 9.5 GB cap)

    payload = {
        "files": [
            {"file_name": "photo.jpg", "file_size": 1024 * 1024 * 10, "content_type": "image/jpeg"}
        ]
    }
    response = client.post("/api/v1/transfers", json=payload)
    assert response.status_code == 413
    assert "Active storage capacity limit reached" in response.json()["detail"]


def test_create_transfer_rate_limiting():
    mock_redis.eval = AsyncMock(return_value=[6, 60000])  # Exceeds rate limit of 5

    payload = {
        "files": [{"file_name": "photo.jpg", "file_size": 100, "content_type": "image/jpeg"}]
    }
    response = client.post("/api/v1/transfers", json=payload)
    assert response.status_code == 429
    assert "Rate limit exceeded" in response.json()["detail"]


def _ready_transfer() -> dict:
    return {
        "transfer_id": "tx_123",
        "sender_secret": "sender-secret",
        "access_token": "access-token",
        "status": "ready",
        "created_at": "2026-07-15T00:00:00Z",
        "expires_at": "2026-07-15T00:30:00Z",
        "total_files": 1,
        "total_size": 100,
        "files": [
            {
                "file_id": "fid_1",
                "file_name": "a.txt",
                "file_size": 100,
                "content_type": "text/plain",
                "status": "uploaded",
            }
        ],
    }


def test_transfer_id_alone_cannot_read_metadata_or_download():
    mock_redis.get_transfer = AsyncMock(return_value=_ready_transfer())
    mock_client = AsyncMock()
    mock_client.ttl = AsyncMock(return_value=1200)
    mock_redis.get_client = AsyncMock(return_value=mock_client)
    mock_r2.generate_download_url = AsyncMock(return_value="https://r2.mock/download")

    bare = client.get("/api/v1/transfers/tx_123")
    wrong = client.get(
        "/api/v1/transfers/tx_123",
        headers={"X-Lynk-Access-Token": "not-the-token"},
    )
    downloads = client.post("/api/v1/transfers/tx_123/downloads", json={})

    assert bare.status_code == 404
    assert wrong.status_code == 404
    assert downloads.status_code == 404
    mock_r2.generate_download_url.assert_not_called()


def test_transfer_id_alone_cannot_cancel_extend_or_complete():
    mock_redis.get_transfer = AsyncMock(return_value=_ready_transfer())
    mock_redis.delete_transfer = AsyncMock()
    mock_redis.extend_transfer = AsyncMock()
    mock_redis.update_transfer = AsyncMock()
    mock_r2.delete_objects = AsyncMock()
    mock_r2.verify_file_exists = AsyncMock(return_value=100)

    cancel = client.delete("/api/v1/transfers/tx_123")
    extend = client.post("/api/v1/transfers/tx_123/extend")
    complete = client.post("/api/v1/transfers/tx_123/files/fid_1/complete")

    assert cancel.status_code == 204
    assert extend.status_code == 404
    assert complete.status_code == 404
    mock_redis.delete_transfer.assert_not_called()
    mock_redis.extend_transfer.assert_not_called()
    mock_r2.delete_objects.assert_not_called()


def test_legacy_record_without_secrets_fails_closed():
    legacy = _ready_transfer()
    legacy["sender_secret"] = ""
    legacy["access_token"] = ""
    mock_redis.get_transfer = AsyncMock(return_value=legacy)

    metadata = client.get(
        "/api/v1/transfers/tx_123",
        headers={"X-Lynk-Access-Token": "access-token"},
    )
    cancel = client.delete(
        "/api/v1/transfers/tx_123",
        headers={"X-Lynk-Sender-Secret": "sender-secret"},
    )
    assert metadata.status_code == 404
    assert cancel.status_code == 204
