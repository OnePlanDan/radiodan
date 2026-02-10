"""
Timeline route — GET /timeline (page) + GET /api/timeline/events (SSE)

DAW-like 3D timeline visualization with Server-Sent Events for live updates.
"""

import asyncio
import json
import time

from aiohttp import web

routes = web.RouteTableDef()


@routes.get("/timeline")
async def timeline_page(request: web.Request) -> web.Response:
    """Render the timeline page (extends base.html)."""
    import aiohttp_jinja2

    env = aiohttp_jinja2.get_env(request.app)
    template = env.get_template("timeline.html")
    station_name = env.globals.get("station_name", "Radio Dan")
    html = template.render(station_name=station_name, page="timeline")
    return web.Response(text=html, content_type="text/html")


@routes.get("/api/timeline/events")
async def timeline_sse(request: web.Request) -> web.StreamResponse:
    """SSE endpoint: sends initial snapshot then streams live updates."""
    event_store = request.app["event_store"]
    stream_context = request.app["stream_context"]

    response = web.StreamResponse(
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)

    # 1. Send snapshot: last 30 minutes + all scheduled future events
    now = time.time()
    window = await event_store.get_window(now - 1800, now + 86400)
    await response.write(f"event: snapshot\ndata: {json.dumps(window)}\n\n".encode())

    # 2. Send current playback state for time synchronization
    planner = stream_context._planner
    crossfade = planner.crossfade_duration if planner else 5.0
    state = {
        "server_time": now,
        "elapsed": stream_context.elapsed_seconds,
        "remaining": stream_context.remaining_seconds,
        "crossfade_duration": crossfade,
    }
    await response.write(f"event: playback_state\ndata: {json.dumps(state)}\n\n".encode())

    # 3. Stream live events with periodic playback state refresh
    queue = event_store.subscribe()
    last_playback_push = time.time()
    try:
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=3)
                await response.write(
                    f"event: event_update\ndata: {json.dumps(msg)}\n\n".encode()
                )
            except asyncio.TimeoutError:
                pass

            # Refresh playback state every ~3 seconds so upcoming tracks stay positioned
            now = time.time()
            if now - last_playback_push >= 3:
                last_playback_push = now
                planner = stream_context._planner
                crossfade = planner.crossfade_duration if planner else 5.0
                pb_state = {
                    "server_time": now,
                    "elapsed": stream_context.elapsed_seconds,
                    "remaining": stream_context.remaining_seconds,
                    "crossfade_duration": crossfade,
                }
                await response.write(
                    f"event: playback_state\ndata: {json.dumps(pb_state)}\n\n".encode()
                )
    except (ConnectionResetError, ConnectionError, asyncio.CancelledError):
        pass
    finally:
        event_store.unsubscribe(queue)

    return response
