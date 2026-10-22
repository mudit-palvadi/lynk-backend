"""A signaling frame must stay small (#31).

A signaling frame is a small JSON message: an SDP offer, an answer, a
candidate. The relay used to accept whatever uvicorn accepted (16 MiB by
default) and keep it in the room history, so one socket could pin tens of
megabytes of API memory and multiply it by opening many rooms. Anything over
the limit now closes the socket instead.
"""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import signaling
from app.main import app


@pytest.fixture()
def client():
    return TestClient(app)


class TestFrameSize:
    def test_a_frame_under_the_limit_is_relayed(self, client):
        with client.websocket_connect("/ws/signaling/frame-ok") as sender:
            with client.websocket_connect("/ws/signaling/frame-ok") as receiver:
                sender.send_json({"type": "offer", "sdp": "s" * 1000})
                assert receiver.receive_json()["sdp"] == "s" * 1000

    def test_a_frame_over_the_limit_closes_the_socket(self, client):
        with client.websocket_connect("/ws/signaling/frame-big") as ws:
            ws.send_text("x" * (signaling.MAX_FRAME_BYTES + 1))
            assert ws.receive_json() == {"type": "error", "msg": "frame too large"}
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()

    def test_an_oversized_frame_never_reaches_the_history(self, client):
        with client.websocket_connect("/ws/signaling/frame-history") as ws:
            ws.send_text("x" * (signaling.MAX_FRAME_BYTES + 1))
            ws.receive_json()
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()

        # The oversized frame must not have been kept for late peers.
        room = signaling.rooms.get("frame-history")
        assert room is not None
        assert room["history"] == []
