"""
Greeter and statistics routes.

The greeter reacts to a listener connecting; these endpoints exist so its
behaviour can be observed and rehearsed without waiting for a real arrival.
"""

import logging

from aiohttp import web

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


def _greeter(request: web.Request):
    greeter = request.app.get("greeter")
    if greeter is None:
        raise web.HTTPServiceUnavailable(reason="Greeter is not enabled")
    return greeter


@routes.get("/api/greeter")
async def greeter_state(request: web.Request) -> web.Response:
    """Greeter state: recent greetings, today's bulletin, current settings."""
    greeter = _greeter(request)
    bulletin = await greeter._bulletin_state()
    bulletin.pop("row", None)
    return web.json_response({
        "enabled": greeter.enabled,
        "listeners_now": greeter._last_listeners,
        "greetings_sent_since_start": greeter.greetings_sent,
        "first_of_day_done": await greeter._first_of_day_done(),
        "todays_bulletin": bulletin,
        "recent": await greeter.recent(10),
        "settings": {
            "listener_name": greeter.listener_name,
            "poll_interval": greeter.poll_interval,
            "cooldown_seconds": greeter.cooldown_seconds,
            "news_show": greeter.news_show,
            "news_hour": greeter.news_hour,
            "first_connect_episode": greeter.first_connect_episode,
        },
    })


@routes.post("/api/greeter/test")
async def greeter_test(request: web.Request) -> web.Response:
    """Fire a greeting now, ignoring the cooldown. Body: {"first_of_day": bool}."""
    greeter = _greeter(request)
    first = False
    try:
        body = await request.json()
        first = bool(body.get("first_of_day"))
    except Exception:
        pass
    result = await greeter.greet(force=True, force_first_of_day=first)
    return web.json_response({"ok": result.get("greeted", False), **result})


@routes.get("/api/stats")
async def station_stats(request: web.Request) -> web.Response:
    """Live station statistics: uptime, disk, library, plays, listeners."""
    stats = request.app.get("station_stats")
    if stats is None:
        raise web.HTTPServiceUnavailable(reason="Stats service is not running")
    return web.json_response(await stats.snapshot())
