"""Shared helpers for API route handlers."""

from aiohttp import web


def get_planner(request: web.Request):
    """Retrieve the PlaylistPlanner from the stream context."""
    ctx = request.app["stream_context"]
    planner = ctx._planner
    if planner is None:
        raise web.HTTPServiceUnavailable(reason="Playlist planner not available")
    return planner


def get_service(request: web.Request, key: str):
    """Retrieve a service from ctx_kwargs by key."""
    service = request.app.get("ctx_kwargs", {}).get(key)
    if service is None:
        raise web.HTTPServiceUnavailable(reason=f"Service not available: {key}")
    return service
