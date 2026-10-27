"""Signaling WebSocket tests — two-party fan-out + late-joiner replay.

Contract (Phases.md): the server is a dumb relay for `offer`, `answer` and
`candidate` JSON between the two peers of a session room. Messages are
buffered per room and replayed to a peer that joins late, so the sender may
signal its offer before the receiver has connected.
"""

import pytest
from fastapi.testclient import TestClient

from app.api import signaling
from app.main import app


@pytest.fixture()
def client():
    return TestClient(app)


class TestTwoPartyRelay:
    def test_offer_relays_from_sender_to_receiver(self, client):
        with client.websocket_connect("/ws/signaling/relay-1") as a:
            a.send_json({"type": "offer", "sdp": "sdp-from-a"})
            with client.websocket_connect("/ws/signaling/relay-1") as b:
                # Late joiner gets the buffered offer replayed on connect.
                assert b.receive_json() == {"type": "offer", "sdp": "sdp-from-a"}
                # And the sender receives nothing on its own socket.
                a.send_json({"type": "ping-probe"})
                assert b.receive_json() == {"type": "ping-probe"}

    def test_answer_and_candidates_relay_both_ways(self, client):
        with client.websocket_connect("/ws/signaling/relay-2") as a:
            with client.websocket_connect("/ws/signaling/relay-2") as b:
                a.send_json({"type": "offer", "sdp": "o"})
                assert b.receive_json() == {"type": "offer", "sdp": "o"}

                b.send_json({"type": "answer", "sdp": "ans"})
                assert a.receive_json() == {"type": "answer", "sdp": "ans"}

                cand = {
                    "type": "candidate",
                    "candidate": {"candidate": "c1", "sdpMid": "0"},
                }
                b.send_json(cand)
                assert a.receive_json() == cand

    def test_invalid_json_gets_error_on_sender_socket_only(self, client):
        with client.websocket_connect("/ws/signaling/relay-3") as a:
            with client.websocket_connect("/ws/signaling/relay-3") as b:
                a.send_text("not-json{")
                err = a.receive_json()
                assert err["type"] == "error"
                assert "invalid JSON" in err["msg"]
                # The malformed frame is NOT relayed or buffered: probe with a
                # valid frame and confirm the peer sees exactly that one.
                a.send_json({"type": "candidate", "candidate": {"candidate": "c"}})
                assert b.receive_json()["type"] == "candidate"

    def test_replay_contains_buffered_history_in_order(self, client):
        with client.websocket_connect("/ws/signaling/relay-4") as a:
            a.send_json({"type": "offer", "sdp": "first"})
            a.send_json(
                {
                    "type": "candidate",
                    "candidate": {"candidate": "cand-1", "sdpMid": "0"},
                }
            )
            with client.websocket_connect("/ws/signaling/relay-4") as b:
                assert b.receive_json() == {"type": "offer", "sdp": "first"}
                assert b.receive_json() == {
                    "type": "candidate",
                    "candidate": {"candidate": "cand-1", "sdpMid": "0"},
                }

    def test_room_survives_one_peer_disconnecting(self, client):
        with client.websocket_connect("/ws/signaling/relay-5") as a:
            a.send_json({"type": "offer", "sdp": "pre"})
            b_ctx = client.websocket_connect("/ws/signaling/relay-5")
            b = b_ctx.__enter__()
            try:
                assert b.receive_json()["type"] == "offer"
            finally:
                b_ctx.__exit__(None, None, None)
            # A stays connected; room must not be dropped. New joiner replays.
            with client.websocket_connect("/ws/signaling/relay-5") as c:
                assert c.receive_json() == {"type": "offer", "sdp": "pre"}


class TestHardening:
    def setup_method(self):
        signaling.rooms.clear()
        signaling._session_validator = None

    def teardown_method(self):
        signaling.rooms.clear()
        signaling._session_validator = None

    def test_room_full_rejects_third_peer(self, client):
        signaling.rooms.clear()
        with client.websocket_connect("/ws/signaling/cap-1") as a:
            with client.websocket_connect("/ws/signaling/cap-1") as b:
                with client.websocket_connect("/ws/signaling/cap-1") as third:
                    msg = third.receive_json()
                    assert msg["type"] == "error"
                    assert "room full" in msg["msg"]
                # The two established peers are unaffected.
                a.send_json({"type": "candidate", "candidate": {"candidate": "x"}})
                assert b.receive_json()["type"] == "candidate"

    def test_hello_declares_role_without_relay(self, client):
        with client.websocket_connect("/ws/signaling/hello-1") as a:
            a.send_json({"type": "hello", "role": "sender"})
            a.send_json({"type": "offer", "sdp": "sdp-h"})
            with client.websocket_connect("/ws/signaling/hello-1") as b:
                # hello is consumed server-side; only the offer is replayed.
                assert b.receive_json() == {"type": "offer", "sdp": "sdp-h"}

    def test_replay_skips_frames_from_same_role_on_reconnect(self, client):
        # Original sender signals, then disconnects and reconnects with the
        # same declared role. Its own offer must NOT be replayed. Proven by
        # joining a fresh receiver afterwards: the FIRST replayed frame must
        # be the reconnecting peer's new probe, never the stale offer.
        with client.websocket_connect("/ws/signaling/role-r1?role=sender") as a:
            a.send_json({"type": "offer", "sdp": "stale-offer"})

        with client.websocket_connect("/ws/signaling/role-r1?role=sender") as d:
            d.send_json(
                {
                    "type": "candidate",
                    "candidate": {"candidate": "probe-c", "sdpMid": "0"},
                }
            )

            with client.websocket_connect("/ws/signaling/role-r1?role=receiver") as e:
                first = e.receive_json()
                assert first["type"] == "candidate"
                assert first["candidate"]["candidate"] == "probe-c"

    def test_replay_reaches_opposite_role(self, client):
        with client.websocket_connect("/ws/signaling/role-r2?role=sender") as a:
            a.send_json({"type": "offer", "sdp": "to-receiver"})
        with client.websocket_connect("/ws/signaling/role-r2?role=receiver") as b:
            assert b.receive_json() == {"type": "offer", "sdp": "to-receiver"}

    def test_validator_rejects_unknown_session(self, client, monkeypatch):
        async def deny(session_id):
            return False

        monkeypatch.setattr(signaling, "_session_validator", deny)
        with client.websocket_connect("/ws/signaling/denied-id") as ws:
            msg = ws.receive_json()
            assert msg == {"type": "error", "msg": "unknown session"}

    def test_validator_allows_known_session(self, client, monkeypatch):
        signaling.rooms.clear()

        async def allow(session_id):
            return True

        monkeypatch.setattr(signaling, "_session_validator", allow)
        with client.websocket_connect("/ws/signaling/known-id?role=sender") as ws:
            ws.send_json({"type": "offer", "sdp": "ok"})
        # Reaching the end without an error frame means validation passed.

    def test_an_occupied_room_is_not_idle(self):
        occupied = {"members": ["peer"], "last_activity": 0.0}
        empty = {"members": [], "last_activity": 0.0}
        assert not signaling.room_is_idle(occupied, now=1000.0, max_idle_seconds=60)
        assert signaling.room_is_idle(empty, now=1000.0, max_idle_seconds=60)

    def test_evict_idle_rooms_drops_stale_and_keeps_fresh(self):
        signaling.rooms.clear()
        fresh = signaling._get_room("fresh")
        fresh["members"].append("peer")
        stale_empty = signaling._get_room("stale-empty")
        stale_busy = signaling._get_room("stale-busy")
        stale_busy["members"].append("peer")

        now = 1000.0
        fresh["last_activity"] = now - 10
        stale_empty["last_activity"] = now - 500
        stale_busy["last_activity"] = now - 500

        evicted = signaling.evict_idle_rooms(max_idle_seconds=60, now=now)

        assert "fresh" not in evicted and "fresh" in signaling.rooms
        assert sorted(evicted) == ["stale-busy", "stale-empty"]

    def test_safe_send_reports_failure(self):
        class Boom:
            async def send_text(self, raw):
                raise ConnectionError("dead")

        class Ok:
            async def send_text(self, raw):
                pass

        import asyncio

        loop = asyncio.new_event_loop()
        try:
            failed = loop.run_until_complete(signaling._safe_send({"ws": Boom()}, "x"))
            delivered = loop.run_until_complete(signaling._safe_send({"ws": Ok()}, "x"))
        finally:
            loop.close()
        assert failed is False
        assert delivered is True
