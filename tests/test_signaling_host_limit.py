"""One client address may hold only so many signaling sockets (#31).

Every accepted socket costs a room slot, a history buffer and a task, and the
relay opens a room for any id, so without a per-address limit a single client
could exhaust the process's memory and file descriptors.
"""

from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.api import signaling
from app.main import app


@pytest.fixture()
def client():
    return TestClient(app)


class TestHostLimit:
    def test_an_address_cannot_hold_unbounded_sockets(self, client):
        # The rest of the suite closes its sockets, so anything here would be
        # a leak from an earlier test.
        assert signaling._open_by_host == {}

        with ExitStack() as stack:
            for i in range(signaling._MAX_SOCKETS_PER_HOST):
                stack.enter_context(client.websocket_connect(f"/ws/signaling/host-{i}"))

            with client.websocket_connect("/ws/signaling/host-overflow") as overflow:
                assert overflow.receive_json() == {
                    "type": "error",
                    "msg": "too many connections",
                }
                with pytest.raises(WebSocketDisconnect):
                    overflow.receive_json()

        assert signaling._open_by_host == {}

    def test_closing_a_socket_frees_its_slot(self, client):
        assert signaling._open_by_host == {}

        with client.websocket_connect("/ws/signaling/host-free"):
            assert signaling._open_by_host != {}

        assert signaling._open_by_host == {}
