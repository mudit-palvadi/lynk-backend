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
from app.core import security
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


class TestCloudflareAddress:
    """#43: behind Cloudflare every socket shares one edge address.

    The cap used to key on the ASGI peer, so ten sockets from anywhere could
    fill the bucket that every user on the same edge address shares.
    """

    def test_the_cap_follows_the_forwarded_client_address(self, client, monkeypatch):
        monkeypatch.setattr(security, "_is_cloudflare_address", lambda _ip: True)

        with ExitStack() as stack:
            for i in range(signaling._MAX_SOCKETS_PER_HOST):
                stack.enter_context(
                    client.websocket_connect(
                        f"/ws/signaling/cf-{i}",
                        headers={"CF-Connecting-IP": "203.0.113.10"},
                    )
                )
            assert signaling._open_by_host["203.0.113.10"] == (
                signaling._MAX_SOCKETS_PER_HOST
            )

            # A different real client on the same edge address still gets in.
            stack.enter_context(
                client.websocket_connect(
                    "/ws/signaling/cf-other",
                    headers={"CF-Connecting-IP": "198.51.100.23"},
                )
            )

        assert signaling._open_by_host == {}

    def test_a_full_forwarded_address_is_still_refused(self, client, monkeypatch):
        monkeypatch.setattr(security, "_is_cloudflare_address", lambda _ip: True)
        headers = {"CF-Connecting-IP": "203.0.113.10"}

        with ExitStack() as stack:
            for i in range(signaling._MAX_SOCKETS_PER_HOST):
                stack.enter_context(
                    client.websocket_connect(f"/ws/signaling/cf-full-{i}", headers=headers)
                )

            with client.websocket_connect(
                "/ws/signaling/cf-overflow", headers=headers
            ) as overflow:
                assert overflow.receive_json() == {
                    "type": "error",
                    "msg": "too many connections",
                }
                with pytest.raises(WebSocketDisconnect):
                    overflow.receive_json()

        assert signaling._open_by_host == {}
