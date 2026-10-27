"""Endpoint tests for TURN credential distribution."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import TransferNotFoundException
from app.main import app
from app.services.redis_service import redis_service
from app.services.transfer_service import TransferService


@pytest.fixture()
def client():
    return TestClient(app)


class TestTurnCredentialsEndpoint:
    def test_returns_credentials_when_turn_configured(self, client):
        # The access check is patched on the class, not on the service
        # instance: another test module leaves a global dependency override
        # for get_transfer_service, so the instance the route receives is not
        # necessarily a fresh one.
        with patch("app.api.transfers.settings") as mock_settings, patch(
            "app.services.transfer_service.TransferService.require_access",
            new=AsyncMock(return_value=None),
        ) as require_access:
            mock_settings.TURN_URLS = "turn:turn.lynkshare.app:3478"
            mock_settings.TURN_SECRET = "endpoint-secret"
            resp = client.get(
                "/api/v1/transfers/some-id/turn-credentials",
                headers={"X-Lynk-Access-Token": "transfer-token"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["urls"] == ["turn:turn.lynkshare.app:3478"]
        assert "username" in body and "credential" in body
        assert "endpoint-secret" not in resp.text
        # #47: the route hands the caller's token to the service, which is the
        # side that decides whether it is right.
        require_access.assert_awaited_once_with(
            "some-id", access_token="transfer-token"
        )

    def test_disabled_when_turn_not_configured(self, client):
        with patch("app.api.transfers.settings") as mock_settings:
            mock_settings.TURN_URLS = ""
            mock_settings.TURN_SECRET = ""
            resp = client.get("/api/v1/transfers/some-id/turn-credentials")

        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    def test_unknown_transfer_gets_no_credentials(self, client):
        """POTENTIAL_ISSUES #25: no relay credentials without a real session."""
        with patch("app.api.transfers.settings") as mock_settings, patch(
            "app.services.transfer_service.TransferService.require_access",
            new=AsyncMock(side_effect=TransferNotFoundException()),
        ):
            mock_settings.TURN_URLS = "turn:turn.lynkshare.app:3478"
            mock_settings.TURN_SECRET = "endpoint-secret"
            resp = client.get("/api/v1/transfers/made-up-id/turn-credentials")

        assert resp.status_code == 404
        assert "credential" not in resp.text
        assert "username" not in resp.text

    def test_a_bad_token_gets_no_credentials(self, client):
        with patch("app.api.transfers.settings") as mock_settings, patch(
            "app.services.transfer_service.TransferService.require_access",
            new=AsyncMock(side_effect=TransferNotFoundException()),
        ):
            mock_settings.TURN_URLS = "turn:turn.lynkshare.app:3478"
            mock_settings.TURN_SECRET = "endpoint-secret"
            resp = client.get("/api/v1/transfers/some-id/turn-credentials")

        assert resp.status_code == 404
        assert "username" not in resp.text
        assert "turn:" not in resp.text


class TestRequireAccess:
    """The check behind the endpoint (#47)."""

    @staticmethod
    def _service() -> TransferService:
        return TransferService(redis_service=redis_service, r2_service=None)

    @pytest.mark.anyio
    async def test_a_live_transfer_accepts_its_token(self):
        with patch.object(
            redis_service,
            "get_transfer",
            new=AsyncMock(return_value={"access_token": "tok"}),
        ):
            await self._service().require_access("t1", access_token="tok")

    @pytest.mark.anyio
    async def test_a_wrong_or_missing_token_is_refused(self):
        with patch.object(
            redis_service,
            "get_transfer",
            new=AsyncMock(return_value={"access_token": "tok"}),
        ):
            service = self._service()
            with pytest.raises(TransferNotFoundException):
                await service.require_access("t1", access_token="nope")
            with pytest.raises(TransferNotFoundException):
                await service.require_access("t1", access_token=None)

    @pytest.mark.anyio
    async def test_an_unknown_transfer_is_not_found(self):
        with patch.object(
            redis_service, "get_transfer", new=AsyncMock(return_value=None)
        ):
            with pytest.raises(TransferNotFoundException):
                await self._service().require_access("made-up", access_token="tok")
