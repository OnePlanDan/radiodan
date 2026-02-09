"""
Queue management routes — GET/POST/DELETE /api/queue

Allows multiple actors (web UI, Telegram, plugins, agents) to view
and manipulate the upcoming music queue.
"""

import json
import logging

from aiohttp import web

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


def _get_planner(request: web.Request):
    """Retrieve the PlaylistPlanner from the stream context."""
    ctx = request.app["stream_context"]
    planner = ctx._planner
    if planner is None:
        raise web.HTTPServiceUnavailable(text="Playlist planner not available")
    return planner


@routes.get("/api/queue")
async def get_queue(request: web.Request) -> web.Response:
    """Return the current upcoming queue as JSON."""
    planner = _get_planner(request)
    upcoming = [
        {
            "position": i,
            "artist": t.get("artist", ""),
            "title": t.get("title", ""),
            "duration_seconds": t.get("duration_seconds", 0),
            "file_path": t.get("file_path", ""),
        }
        for i, t in enumerate(planner.upcoming)
    ]
    return web.json_response({"queue": upcoming, "count": len(upcoming)})


@routes.post("/api/queue")
async def insert_track(request: web.Request) -> web.Response:
    """Insert a track into the queue.

    Body: {"file_path": "/path/to/song.mp3", "position": N}
    position is optional (defaults to append).
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="Invalid JSON")

    file_path = body.get("file_path")
    if not file_path:
        raise web.HTTPBadRequest(text="file_path is required")

    position = body.get("position")
    if position is not None:
        try:
            position = int(position)
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="position must be an integer")

    planner = _get_planner(request)
    success = await planner.insert_track(file_path, position)

    if not success:
        raise web.HTTPNotFound(text="Track not found in music library")

    return web.json_response({"ok": True, "queue_length": len(planner.upcoming)})


@routes.delete("/api/queue/{position}")
async def remove_track(request: web.Request) -> web.Response:
    """Remove a track from the queue by position."""
    try:
        position = int(request.match_info["position"])
    except (ValueError, KeyError):
        raise web.HTTPBadRequest(text="Invalid position")

    planner = _get_planner(request)
    removed = await planner.remove_track(position)

    if removed is None:
        raise web.HTTPNotFound(text=f"No track at position {position}")

    return web.json_response({
        "ok": True,
        "removed": {
            "artist": removed.get("artist", ""),
            "title": removed.get("title", ""),
            "file_path": removed.get("file_path", ""),
        },
        "queue_length": len(planner.upcoming),
    })


@routes.post("/api/queue/reorder")
async def reorder_track(request: web.Request) -> web.Response:
    """Move a track from one position to another.

    Body: {"from": N, "to": M}
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="Invalid JSON")

    try:
        from_pos = int(body["from"])
        to_pos = int(body["to"])
    except (KeyError, TypeError, ValueError):
        raise web.HTTPBadRequest(text="'from' and 'to' integer fields are required")

    planner = _get_planner(request)
    success = await planner.move_track(from_pos, to_pos)

    if not success:
        raise web.HTTPBadRequest(text="Invalid positions")

    return web.json_response({"ok": True, "queue_length": len(planner.upcoming)})
