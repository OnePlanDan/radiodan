"""
Events SSE route — GET /api/events

Server-Sent Events stream for real-time updates.
"""

import asyncio
import json
import time

from aiohttp import web

routes = web.RouteTableDef()


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
