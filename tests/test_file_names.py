"""File names are chosen by the sender and written to disk by every receiver.

A name that is not a single path segment would let a sender write outside the
download directory, so it is refused at the API boundary, before it is ever
stored or served.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.api.transfers import get_transfer_service
from app.core.file_names import MAX_FILE_NAME_BYTES, validate_file_name
from app.main import app
from app.services.redis_service import redis_service
from app.services.transfer_service import TransferService

mock_redis = AsyncMock()
mock_redis.incr = AsyncMock(return_value=1)
mock_redis.get_client = AsyncMock(return_value=mock_redis)
mock_redis.zrange = AsyncMock(return_value=[])
redis_service.get_client = AsyncMock(return_value=mock_redis)
mock_r2 = AsyncMock()

mock_transfer_service = TransferService(
    redis_service=mock_redis, r2_service=mock_r2
)

app.dependency_overrides.pop(get_transfer_service, None)

client = TestClient(app)


@pytest.fixture(autouse=True)
def use_mock_transfer_service():
    """Point the API at this module's service.

    The override is installed per test rather than at import time: other test
    modules also replace this dependency at import time, and the last one
    imported would otherwise decide which service these tests talk to.
    """
    original = app.dependency_overrides.get(get_transfer_service)
    app.dependency_overrides[get_transfer_service] = lambda: mock_transfer_service
    yield
    if original is None:
        app.dependency_overrides.pop(get_transfer_service, None)
    else:
        app.dependency_overrides[get_transfer_service] = original


@pytest.fixture(autouse=True)
def run_around_tests():
    mock_redis.reset_mock()
    mock_redis.incr = AsyncMock(return_value=1)
    mock_redis.get_client = AsyncMock(return_value=mock_redis)
    mock_redis.zrange = AsyncMock(return_value=[])
    mock_r2.reset_mock()
    yield


def _post(file_name: str):
    return client.post(
        "/api/v1/transfers",
        json={
            "files": [
                {
                    "file_name": file_name,
                    "file_size": 1024,
                    "content_type": "application/octet-stream",
                }
            ]
        },
    )


@pytest.mark.parametrize(
    "name",
    [
        "../etc/passwd",
        "..",
        "../../.ssh/authorized_keys",
        "folder/file.txt",
        "folder\\file.txt",
        "/etc/passwd",
        "C:\\Windows\\System32\\config",
        "with\x00null.txt",
        "with\nnewline.txt",
        "with\ttab.txt",
        "",
        "   ",
        " leading-space.txt",
        "trailing-space.txt ",
        "report.txt:stream",
        "a" * (MAX_FILE_NAME_BYTES + 1),
        "é" * (MAX_FILE_NAME_BYTES // 2 + 1),
    ],
)
def test_validate_file_name_rejects_unsafe_names(name):
    with pytest.raises(ValueError):
        validate_file_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "photo.jpg",
        "a",
        "file with spaces.txt",
        "archive.tar.gz",
        ".gitignore",
        "ünïcödé.txt",
        "a" * MAX_FILE_NAME_BYTES,
        "é" * (MAX_FILE_NAME_BYTES // 2),
    ],
)
def test_validate_file_name_accepts_ordinary_names(name):
    assert validate_file_name(name) == name


def test_validate_file_name_counts_bytes_not_characters():
    """A multi-byte name can be far shorter in characters than in bytes."""
    assert len("é" * (MAX_FILE_NAME_BYTES // 2)) < MAX_FILE_NAME_BYTES
    with pytest.raises(ValueError):
        validate_file_name("é" * (MAX_FILE_NAME_BYTES // 2 + 1))


@pytest.mark.parametrize(
    "name",
    [
        "../etc/passwd",
        "..",
        "folder/file.txt",
        "folder\\file.txt",
        "/etc/passwd",
        "with\x00null.txt",
        "report.txt:stream",
    ],
)
def test_create_transfer_refuses_an_unsafe_file_name(name):
    mock_redis.set_transfer = AsyncMock()

    response = _post(name)

    assert response.status_code == 400
    # The message has to be a plain string: the clients show it verbatim.
    assert isinstance(response.json()["detail"], str)
    mock_redis.set_transfer.assert_not_called()


def test_create_transfer_still_accepts_an_ordinary_file_name():
    mock_redis.set_transfer = AsyncMock()
    mock_r2.generate_upload_url = AsyncMock(return_value="https://r2.mock/upload")

    response = _post("photo.jpg")

    assert response.status_code == 201
    mock_redis.set_transfer.assert_called_once()
