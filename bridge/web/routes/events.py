"""
Event routes.

  GET /api/events          — Server-Sent Events stream for real-time updates
  GET /api/events/history  — plain JSON window of what already aired

The SSE stream pushes a fixed 30-minute snapshot on connect, which suits a live
dashboard but not an agent asking a one-off question: it would have to hold a
streaming connection open, parse SSE frames, and accept whatever window it was
given. `/history` answers that in one request with a window the caller chooses.
"""

import asyncio
import json
import time

from aiohttp import web

routes = web.RouteTableDef()

# Bounds on the history query. The event log holds months of rows (64 000+ by
# mid-2026), so an unbounded call is a footgun for both sides.
_DEFAULT_MINUTES = 60.0
_MAX_MINUTES = 1440.0
_DEFAULT_LIMIT = 200
_MAX_LIMIT = 1000


def _clamped(raw: str | None, default: float, low: float, high: float) -> float:
    """Parse a query number, falling back to the default on anything unusable."""
    if raw is None:
        return default
    try:
        return max(low, min(high, float(raw)))
    except (TypeError, ValueError):
        return default


@routes.get("/api/events/history")
async def events_history(request: web.Request) -> web.Response:
    """What aired recently, as plain JSON. Query: minutes, limit, lane."""
    event_store = request.app["event_store"]

    minutes = _clamped(request.query.get("minutes"), _DEFAULT_MINUTES, 1.0, _MAX_MINUTES)
    limit = int(_clamped(request.query.get("limit"), _DEFAULT_LIMIT, 1, _MAX_LIMIT))
    lanes = request.query.getall("lane", None) or None

    now = time.time()
    start = now - minutes * 60
    # end slightly ahead of now so an in-flight event is included rather than
    # dropped for not having started yet.
    events = await event_store.get_window(start, now + 1.0, lanes=lanes)

    # get_window uses overlap semantics: an event with no ended_at counts as still
    # running and matches any window, however old. That is right for a live
    # timeline and wrong here — the station has hundreds of rows that were never
    # closed (voice segments stranded in `scheduled`), and they would surface in
    # every history call forever. "What aired in the last hour" means what
    # *started* in it.
    events = [e for e in events if e.get("started_at", 0) >= start]

    # Newest first, because "what just happened" is the usual question.
    events.reverse()
    truncated = len(events) > limit

    return web.json_response({
        "window": {
            "minutes": minutes,
            "from": start,
            "to": now,
            "lanes": lanes,
        },
        "count": min(len(events), limit),
        "truncated": truncated,
        "events": events[:limit],
    })


@routes.get("/api/events")
async def events_sse(request: web.Request) -> web.StreamResponse:
    """SSE: snapshot, now_playing, event updates, heartbeat every 3s."""
    event_store = request.app["event_store"]
    stream_context = request.app["stream_context"]

    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Access-Control-Allow-Origin": "*",
        },
    )
    response.headers["retry"] = "3000"
    await response.prepare(request)

    # 1. Send snapshot of recent events
    now = time.time()
    window = await event_store.get_window(now - 1800, now + 86400)
    await response.write(f"event: snapshot\ndata: {json.dumps(window)}\n\n".encode())

    # 2. Send current playback state
    planner = stream_context._planner
    crossfade = planner.crossfade_duration if planner else 5.0
    track = stream_context.current_track or {}

    now_playing = {
        "server_time": now,
        "elapsed": stream_context.elapsed_seconds,
        "remaining": stream_context.remaining_seconds,
        "crossfade_duration": crossfade,
        "artist": track.get("artist", ""),
        "title": track.get("title", ""),
    }
    await response.write(f"event: now_playing\ndata: {json.dumps(now_playing)}\n\n".encode())

    # 3. Stream live events + periodic heartbeat
    queue = event_store.subscribe()
    last_heartbeat = time.time()
    try:
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=3)
                await response.write(
                    f"event: update\ndata: {json.dumps(msg)}\n\n".encode()
                )
            except asyncio.TimeoutError:
                pass

            now = time.time()
            if now - last_heartbeat >= 3:
                last_heartbeat = now
                planner = stream_context._planner
                crossfade = planner.crossfade_duration if planner else 5.0
                heartbeat = {
                    "server_time": now,
                    "elapsed": stream_context.elapsed_seconds,
                    "remaining": stream_context.remaining_seconds,
                    "crossfade_duration": crossfade,
                }
                await response.write(
                    f"event: heartbeat\ndata: {json.dumps(heartbeat)}\n\n".encode()
                )
    except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
        pass
    finally:
        event_store.unsubscribe(queue)

    return response
