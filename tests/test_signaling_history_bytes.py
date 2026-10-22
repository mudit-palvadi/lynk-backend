"""Signaling history is capped by bytes, not only by frame count (#31).

A room keeps the last frames so a peer that joins late still gets the offer.
The frame cap alone allowed a hundred frames of up to 16 KB each, so a room
could hold 1.6 MB of history, and any id opens a room. The byte cap bounds a
room no matter how large the frames are.
"""

import pytest
from fastapi.testclient import TestClient

from app.api import signaling
from app.main import app


@pytest.fixture()
def client():
    return TestClient(app)


def _total_chars(room: dict) -> int:
    return sum(len(raw) for _, raw in room["history"])


class TestTrimHistory:
    """The trimming helper itself, without going through the socket."""

    def test_the_byte_cap_bounds_a_room(self):
        room = {"history": [(None, "x" * 100_000) for _ in range(10)]}

        signaling._trim_history(room)

        assert _total_chars(room) <= signaling._HISTORY_BYTES_CAP
        assert room["history"], "the room kept nothing to replay"

    def test_the_newest_frames_survive(self):
        room = {
            "history": [(None, f"frame-{i}-" + "x" * 50_000) for i in range(10)],
        }

        signaling._trim_history(room)

        # Trimming drops from the front, so the oldest frame goes and the
        # conversation the late peer needs is still there.
        assert room["history"][-1][1].startswith("frame-9-")
        assert not room["history"][0][1].startswith("frame-0-")

    def test_the_frame_cap_still_applies(self):
        room = {"history": [(None, "tiny") for _ in range(150)]}

        signaling._trim_history(room)

        assert len(room["history"]) == signaling._HISTORY_CAP


class TestHistoryOverTheRelay:
    def test_many_large_frames_do_not_grow_the_room(self, client):
        frame = {"type": "candidate", "c": "x" * 8_000}
        with client.websocket_connect("/ws/signaling/history-bytes") as a:
            with client.websocket_connect("/ws/signaling/history-bytes") as b:
                for _ in range(40):
                    a.send_json(frame)
                # Each relayed frame is proof the server stored it, so after
                # this loop the whole batch has been through the relay.
                for _ in range(40):
                    b.receive_json()

        room = signaling.rooms["history-bytes"]
        assert _total_chars(room) <= signaling._HISTORY_BYTES_CAP
        assert room["history"], "the room kept nothing to replay"
