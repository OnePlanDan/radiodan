"""
Dashboard route — GET /

Shows current track, service health, active plugin instances, system status.
"""

import aiohttp_jinja2
from aiohttp import web

from bridge.web.routes.system import get_process_info

routes = web.RouteTableDef()


@routes.get("/")
@aiohttp_jinja2.template("dashboard.html")
async def dashboard(request: web.Request) -> dict:
    """Render the dashboard page."""
    mixer = request.app["mixer"]
    stream_context = request.app["stream_context"]
    plugins = request.app["plugins"]

    # Current track info
    track = stream_context.current_track or {}
    remaining = stream_context.remaining_seconds
    elapsed = stream_context.elapsed_seconds

    # System process info (Python bridge + Docker containers)
    processes = await get_process_info(request.app)

    # Active plugin instances
    active_plugins = [
        {
            "instance_id": p.instance_id,
            "display_name": p.display_name,
            "name": p.name,
            "version": p.version,
        }
        for p in plugins
    ]

    return {
        "page": "dashboard",
        "track": track,
        "remaining": remaining,
        "elapsed": elapsed,
        "processes": processes,
        "plugins": active_plugins,
    }
