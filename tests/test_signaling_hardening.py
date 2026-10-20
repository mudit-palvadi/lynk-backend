"""Signaling hardening: session validation and the idle sweeper (#2, #28).

Both pieces of machinery already exist in ``app/api/signaling.py``
(``set_session_validator``, ``start_idle_sweeper``, ``evict_idle_rooms``) but
were never wired up, so any id could open a room and rooms never expired.
These tests cover the wiring in ``app/main.py`` and the validator itself.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.api import signaling as signaling_module
from app.main import _signaling_session_exists, app
from app.services.redis_service import redis_service


def _live_transfer(_transfer_id: str) -> dict:
    return {"transfer_id": _transfer_id, "status": "uploading"}


class TestSessionValidator:
    """The validator accepts both id spaces the clients use."""

    def test_accepts_a_live_transfer_id(self):
        with patch.object(
            redis_service, "get_transfer", new=AsyncMock(return_value=_live_transfer("t1"))
        ):
            assert asyncio.run(_signaling_session_exists("t1")) is True

    def test_accepts_a_live_receiver_session_id(self):
        # The sender pairs with a receiver session when the receiver shares a
        # QR; the room then carries the receiver-session id, not a transfer id.
        with patch.object(
            redis_service, "get_transfer", new=AsyncMock(return_value=None)
        ), patch.object(
            redis_service,
            "get_receiver_session",
            new=AsyncMock(return_value={"session_id": "r1"}),
        ):
            assert asyncio.run(_signaling_session_exists("r1")) is True

    def test_refuses_an_unknown_id(self):
        with patch.object(
            redis_service, "get_transfer", new=AsyncMock(return_value=None)
        ), patch.object(
            redis_service, "get_receiver_session", new=AsyncMock(return_value=None)
        ):
            assert asyncio.run(_signaling_session_exists("made-up")) is False


class TestLifespanWiring:
    """The app lifespan must register the validator and start the sweeper."""

    def test_registers_the_validator_and_runs_the_sweeper(self):
        with TestClient(app):
            assert signaling_module._session_validator is not None
            assert signaling_module._idle_sweeper_task is not None

    def test_stops_the_sweeper_on_shutdown(self):
        with TestClient(app):
            pass
        assert signaling_module._idle_sweeper_task is None
        assert signaling_module._session_validator is None


class TestIdleEviction:
    def test_a_room_idle_beyond_the_window_is_evicted(self):
        signaling_module.rooms["idle-room"] = {
            "members": [],
            "history": [],
            "last_activity": 0.0,
        }
        try:
            evicted = signaling_module.evict_idle_rooms(
                max_idle_seconds=60.0, now=120.0
            )
            assert "idle-room" in evicted
            assert "idle-room" not in signaling_module.rooms
        finally:
            signaling_module.rooms.pop("idle-room", None)

    def test_a_busy_room_survives(self):
        signaling_module.rooms["busy-room"] = {
            "members": [],
            "history": [],
            "last_activity": 100.0,
        }
        try:
            evicted = signaling_module.evict_idle_rooms(
                max_idle_seconds=60.0, now=120.0
            )
            assert "busy-room" not in evicted
            assert "busy-room" in signaling_module.rooms
        finally:
            signaling_module.rooms.pop("busy-room", None)
