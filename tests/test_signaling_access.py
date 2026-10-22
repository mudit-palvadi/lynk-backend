"""Signaling access frames (#32).

A WebSocket handshake from a browser carries no custom header, so the room
credential arrives as the first frame: ``{"type": "auth", "token": "..."}``.
The relay reads it before the socket joins the room and drops it, so a peer
can never read a token out of the replayed history.
"""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api.signaling import set_access_validator
from app.main import app


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_access_validator():
    yield
    set_access_validator(None)


def _recording_validator(seen: list[tuple[str, str]], *, accept: str):
    async def validate(session_id: str, token: str) -> bool:
        seen.append((session_id, token))
        return token == accept

    return validate


class TestAuthFrameIsNeverRelayed:
    def test_an_auth_frame_is_not_forwarded_or_replayed(self, client):
        # No validator is registered, so the relay is permissive -- but a token
        # must still never reach the other peer.
        with client.websocket_connect("/ws/signaling/access-1") as a:
            with client.websocket_connect("/ws/signaling/access-1") as b:
                a.send_json({"type": "auth", "token": "secret"})
                a.send_json({"type": "offer", "sdp": "o"})
                assert b.receive_json() == {"type": "offer", "sdp": "o"}


class TestAccessCheck:
    def test_the_validator_receives_the_room_id_and_token(self, client):
        seen: list[tuple[str, str]] = []
        set_access_validator(_recording_validator(seen, accept="good"))

        with client.websocket_connect("/ws/signaling/access-2") as a:
            a.send_json({"type": "auth", "token": "good"})
            a.send_json({"type": "offer", "sdp": "o"})
            with client.websocket_connect("/ws/signaling/access-2") as b:
                b.send_json({"type": "auth", "token": "good"})
                assert b.receive_json() == {"type": "offer", "sdp": "o"}

        assert seen == [("access-2", "good"), ("access-2", "good")]

    def test_a_socket_that_never_presents_a_token_is_refused(self, client):
        seen: list[tuple[str, str]] = []
        set_access_validator(_recording_validator(seen, accept="good"))

        with client.websocket_connect("/ws/signaling/access-3") as ws:
            ws.send_json({"type": "offer", "sdp": "o"})
            assert ws.receive_json()["msg"] == "auth required"
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()

        assert seen == []

    def test_a_wrong_token_is_refused(self, client):
        seen: list[tuple[str, str]] = []
        set_access_validator(_recording_validator(seen, accept="good"))

        with client.websocket_connect("/ws/signaling/access-4") as ws:
            ws.send_json({"type": "auth", "token": "wrong"})
            assert ws.receive_json()["msg"] == "auth required"
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()

        assert seen == [("access-4", "wrong")]

    def test_a_silent_socket_is_refused_after_the_window(self, client, monkeypatch):
        from app.api import signaling

        monkeypatch.setattr(signaling, "AUTH_TIMEOUT_SECONDS", 0.05)
        set_access_validator(_recording_validator([], accept="good"))

        with client.websocket_connect("/ws/signaling/access-5") as ws:
            assert ws.receive_json()["msg"] == "auth required"
            with pytest.raises(WebSocketDisconnect):
                ws.receive_json()
