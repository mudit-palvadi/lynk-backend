"""WebSocket signaling relay for WebRTC P2P sessions.

 SINGLE-PROCESS CONSTRAINT -- read before scaling.

Room state (`rooms` below) is in-memory and process-local by design. This is
correct ONLY while exactly **one** Uvicorn worker serves the app:

- Sender and receiver MUST land on the same worker or they get separate,
  disconnected rooms  signaling silently never completes.
- State is lost on restart. Harmless for P2P (peers reconnect), but do not
  treat rooms as durable.

Enforcement:
- The Docker image runs `uvicorn ... --port 8000` with NO --workers flag
  (= 1 worker) -- keep it that way until Redis-backed fan-out lands.
- `main.py` logs a loud warning at startup if WEB_CONCURRENCY > 1.

Hardening (audit ISSUES 04-07):
- Optional session validation: set `set_session_validator(fn)` (wired in
  main.py when SIGNALING_VALIDATE_SESSION=true) -- unknown ids are refused.
- Optional access check: set `set_access_validator(fn)` (wired in main.py when
  SIGNALING_REQUIRE_ACCESS=true) -- the first frame must then be
  `{"type":"auth","token":"..."}`. It is consumed before the socket joins the
  room, so the token is never relayed and never replayed from history.
- Rooms are capped at two members; a third connection gets an error frame
  and is closed.
- Fan-out wraps every send: a dying socket is pruned instead of killing the
  healthy peer's loop.
- Rooms idle longer than SIGNALING_IDLE_EVICTION_SECONDS are evicted by a
  background sweeper started from main.py's lifespan.

Roles: clients may declare `"role": "sender"|"receiver"` either via the
`?role=` query parameter or a first-frame hello (`{"type":"hello", ...}`).
History replay skips frames authored by the same declared role, so a
reconnecting peer never receives its own offer back.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.security import resolve_client_address

router = APIRouter(tags=["signaling"])

rooms: dict[str, dict[str, Any]] = {}

_HISTORY_CAP = 100
_MAX_MEMBERS = 2
DEFAULT_IDLE_EVICTION_SECONDS = 60.0

# The most history a room may keep, in bytes.
#
# The frame cap alone is not enough: a hundred frames of the largest accepted
# size is 1.6 MB per room, and any id opens a room. The byte cap keeps a room
# small no matter how large its frames are. Signaling frames are JSON, so the
# frame text length is the byte count for every practical frame.
_HISTORY_BYTES_CAP = 256 * 1024

# The largest frame the relay accepts, in bytes.
#
# A signaling frame is a small JSON message: an SDP offer, an answer, a
# candidate. Anything larger is either a mistake or an attempt to use the
# relay as storage, so the socket is closed instead of the frame being kept.
MAX_FRAME_BYTES = 16 * 1024

# How many signaling sockets one client address may hold at once.
#
# Each socket costs a room slot, a history buffer and a task, and the relay
# accepts any id as a room, so one address must not be able to hold an
# unbounded number of them. The address is the real client address: behind
# Cloudflare every socket shares one edge address, so #43 resolves it the same
# way the HTTP limiter does.
_MAX_SOCKETS_PER_HOST = 10

_open_by_host: dict[str, int] = {}


def _open_host_connection(host: str) -> bool:
    """Reserve a socket slot for a client address; False when it is full."""
    count = _open_by_host.get(host, 0)
    if count >= _MAX_SOCKETS_PER_HOST:
        return False
    _open_by_host[host] = count + 1
    return True


def _close_host_connection(host: str) -> None:
    count = _open_by_host.get(host, 0) - 1
    if count > 0:
        _open_by_host[host] = count
    else:
        _open_by_host.pop(host, None)


_session_validator: Callable[[str], Awaitable[bool]] | None = None


def set_session_validator(fn: Callable[[str], Awaitable[bool]] | None) -> None:
    """Register an async validator; returning False refuses the connection."""
    global _session_validator
    _session_validator = fn


# How long a socket may take to present its access token once one is required.
#
# A WebSocket handshake carries no custom header from a browser, so the
# credential arrives as the first frame instead. The window is short because a
# socket that is waiting is a socket that is holding a host slot and a room.
AUTH_TIMEOUT_SECONDS = 5.0

_access_validator: Callable[[str, str], Awaitable[bool]] | None = None


def set_access_validator(fn: Callable[[str, str], Awaitable[bool]] | None) -> None:
    """Register an async (session_id, token) validator; False refuses the join."""
    global _access_validator
    _access_validator = fn


async def _read_auth_frame(websocket: WebSocket) -> str | None:
    """Return the token in the first frame, or None when it is not an auth frame.

    The frame is read before the socket joins the room and is dropped, so the
    token never enters the history a later peer receives.
    """
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=AUTH_TIMEOUT_SECONDS)
    except (TimeoutError, WebSocketDisconnect):
        return None
    if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
        return None
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict) or decoded.get("type") != "auth":
        return None
    token = decoded.get("token")
    if not isinstance(token, str) or not token:
        return None
    return token


def _get_room(session_id: str) -> dict[str, Any]:
    if session_id not in rooms:
        rooms[session_id] = {
            "members": [],  # [{ws, role}]
            "history": [],  # [(role, raw_frame)]
            "last_activity": time.monotonic(),
        }
    return rooms[session_id]


def _touch(room: dict[str, Any]) -> None:
    room["last_activity"] = time.monotonic()


def _trim_history(room: dict[str, Any]) -> None:
    """Keep the room history short, by frames and by bytes.

    The newest frames are the ones a late peer needs, so trimming drops from
    the front: the room keeps the tail of the conversation.
    """
    history = room["history"]
    if len(history) > _HISTORY_CAP:
        del history[: len(history) - _HISTORY_CAP]
    total = sum(len(raw) for _, raw in history)
    while history and total > _HISTORY_BYTES_CAP:
        _, dropped = history.pop(0)
        total -= len(dropped)


def room_is_idle(
    room: dict[str, Any],
    *,
    now: float,
    max_idle_seconds: float,
) -> bool:
    """An occupied room stays, even when no frame has arrived recently.

    A quiet sender is still in the room. Dropping that room sends the
    receiver into a new empty one, so the two peers never meet.
    """
    if room.get("members"):
        return False
    return now - room["last_activity"] >= max_idle_seconds


def evict_idle_rooms(
    max_idle_seconds: float = DEFAULT_IDLE_EVICTION_SECONDS,
    now: float | None = None,
) -> list[str]:
    """Drop rooms idle beyond the threshold.

    Returns the evicted session ids. Injected `now` keeps it testable.
    History is kept for late joiners until the idle window expires.
    """
    current = time.monotonic() if now is None else now
    evicted = []
    for sid in list(rooms):
        room = rooms[sid]
        if room_is_idle(room, now=current, max_idle_seconds=max_idle_seconds):
            rooms.pop(sid, None)
            evicted.append(sid)
    return evicted


def _prune_member(room: dict[str, Any], ws: WebSocket) -> None:
    room["members"] = [m for m in room["members"] if m["ws"] is not ws]


async def _safe_send(member: dict[str, Any], raw: str) -> bool:
    try:
        await member["ws"].send_text(raw)
        return True
    except Exception:
        return False


def _should_replay(entry_role: str | None, joiner_role: str | None) -> bool:
    """Replay skips frames authored by the joiner's own declared role."""
    if joiner_role is None or entry_role is None:
        return True
    return entry_role != joiner_role


_idle_sweeper_task: asyncio.Task | None = None


def start_idle_sweeper(
    interval_seconds: float = 30.0,
    max_idle_seconds: float = DEFAULT_IDLE_EVICTION_SECONDS,
) -> None:
    global _idle_sweeper_task  # noqa: F823 - module-level task handle

    async def _loop() -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                evict_idle_rooms(max_idle_seconds=max_idle_seconds)
            except Exception:  # pragma: no cover - defensive
                pass

    stop_idle_sweeper()
    _idle_sweeper_task = asyncio.create_task(_loop())


def stop_idle_sweeper() -> None:
    global _idle_sweeper_task
    if _idle_sweeper_task is not None and not _idle_sweeper_task.done():
        _idle_sweeper_task.cancel()
    _idle_sweeper_task = None


@router.websocket("/signaling/{session_id}")
async def signaling_ws(websocket: WebSocket, session_id: str, role: str | None = None) -> None:
    await websocket.accept()

    host = resolve_client_address(
        websocket.client.host if websocket.client else None,
        websocket.headers.get("CF-Connecting-IP"),
    )
    if not _open_host_connection(host):
        await websocket.send_text(json.dumps({"type": "error", "msg": "too many connections"}))
        await websocket.close()
        return

    try:
        if _session_validator is not None and not await _session_validator(session_id):
            await websocket.send_text(json.dumps({"type": "error", "msg": "unknown session"}))
            await websocket.close()
            return

        if _access_validator is not None:
            token = await _read_auth_frame(websocket)
            if token is None or not await _access_validator(session_id, token):
                await websocket.send_text(json.dumps({"type": "error", "msg": "auth required"}))
                await websocket.close()
                return

        room = _get_room(session_id)
        if len(room["members"]) >= _MAX_MEMBERS:
            await websocket.send_text(json.dumps({"type": "error", "msg": "room full"}))
            await websocket.close()
            return

        # Clean up stale history from a previous peer with the same role, so
        # reconnecting senders don't leave ghost offers for future receivers.
        if role is not None:
            room["history"] = [h for h in room["history"] if h[0] != role]
        # Replay buffered history authored by the *opposite* role only, so a
        # reconnecting peer never receives its own offer back (ISSUE-05).
        for entry_role, raw in list(room["history"]):
            if _should_replay(entry_role, role):
                await websocket.send_text(raw)
        room["members"].append({"ws": websocket, "role": role})
        _touch(room)

        try:
            while True:
                raw = await websocket.receive_text()
                _touch(room)
                if len(raw.encode("utf-8")) > MAX_FRAME_BYTES:
                    # Too large to be a signaling message. Close the socket rather
                    # than keep it: the peer is either confused or using the relay
                    # as storage.
                    await websocket.send_text(
                        json.dumps({"type": "error", "msg": "frame too large"})
                    )
                    _prune_member(room, websocket)
                    _touch(room)
                    await websocket.close()
                    return
                try:
                    decoded = json.loads(raw)
                    if not isinstance(decoded, dict):
                        raise ValueError("not an object")
                except (json.JSONDecodeError, ValueError):
                    await websocket.send_text(json.dumps({"type": "error", "msg": "invalid JSON"}))
                    continue

                # The token is consumed before the room is joined; a late one is
                # dropped here so it is never relayed or replayed.
                if decoded.get("type") == "auth":
                    continue

                # In-band role declaration (ISSUE-05): stored, never relayed.
                if decoded.get("type") == "hello":
                    declared = decoded.get("role")
                    for m in room["members"]:
                        if m["ws"] is websocket and declared in ("sender", "receiver"):
                            m["role"] = declared
                            break
                    continue

                room["history"].append((role, raw))
                _trim_history(room)

                dead: list[Any] = []
                for m in list(room["members"]):
                    if m["ws"] is websocket:
                        continue
                    ok = await _safe_send(m, raw)
                    if not ok:
                        dead.append(m["ws"])
                for bad in dead:
                    _prune_member(room, bad)

        except WebSocketDisconnect:
            _prune_member(room, websocket)
            _touch(room)
    finally:
        _close_host_connection(host)
