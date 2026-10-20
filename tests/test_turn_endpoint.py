"""Endpoint tests for TURN credential distribution."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.redis_service import redis_service
from app.services.transfer_service import TransferService


@pytest.fixture()
def client():
    return TestClient(app)


class TestTurnCredentialsEndpoint:
    def test_returns_credentials_when_turn_configured(self, client):
        # The existence check is patched on the class, not on the service
        # instance: another test module leaves a global dependency override
        # for get_transfer_service, so the instance the route receives is not
        # necessarily a fresh one.
        with patch("app.api.transfers.settings") as mock_settings, patch(
            "app.services.transfer_service.TransferService.transfer_exists",
            new=AsyncMock(return_value=True),
        ):
            mock_settings.TURN_URLS = "turn:turn.lynkshare.app:3478"
            mock_settings.TURN_SECRET = "endpoint-secret"
            resp = client.get("/api/v1/transfers/some-id/turn-credentials")

        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["urls"] == ["turn:turn.lynkshare.app:3478"]
        assert "username" in body and "credential" in body
        assert "endpoint-secret" not in resp.text

    def test_disabled_when_turn_not_configured(self, client):
        with patch("app.api.transfers.settings") as mock_settings:
            mock_settings.TURN_URLS = ""
            mock_settings.TURN_SECRET = ""
            resp = client.get("/api/v1/transfers/some-id/turn-credentials")

        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    def test_unknown_transfer_gets_no_credentials(self, client):
        """POTENTIAL_ISSUES #25: no relay credentials without a real session.

        Minting credentials for an id nobody issued would let anyone use the
        relay, so an unknown id must not produce a credential.
        """
        with patch("app.api.transfers.settings") as mock_settings, patch(
            "app.services.transfer_service.TransferService.transfer_exists",
            new=AsyncMock(return_value=False),
        ):
            mock_settings.TURN_URLS = "turn:turn.lynkshare.app:3478"
            mock_settings.TURN_SECRET = "endpoint-secret"
            resp = client.get("/api/v1/transfers/made-up-id/turn-credentials")

        assert resp.status_code == 404
        assert "credential" not in resp.text
        assert "username" not in resp.text


class TestTransferExists:
    """The lookup the endpoint and the signaling validator rely on."""

    @pytest.mark.anyio
    async def test_true_for_a_live_transfer(self):
        service = TransferService(redis_service=redis_service, r2_service=None)
        with patch.object(
            redis_service, "get_transfer", new=AsyncMock(return_value={"transfer_id": "t1"})
        ):
            assert await service.transfer_exists("t1") is True

    @pytest.mark.anyio
    async def test_false_for_an_unknown_transfer(self):
        service = TransferService(redis_service=redis_service, r2_service=None)
        with patch.object(
            redis_service, "get_transfer", new=AsyncMock(return_value=None)
        ):
            assert await service.transfer_exists("made-up") is False
