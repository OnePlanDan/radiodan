"""
Settings — every knob the station has, visible and turnable.

House rule: anything configurable ships with a GUI surface. An API-only
setting is invisible to the person running the station and effectively does
not exist. `/settings` is where new knobs land unless they belong to a more
specific page.

Changes apply live and persist to the config store (SQLite), which main.py
replays over the YAML config at boot — so a turned knob survives a restart
without editing station.yaml.
"""

import logging
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()

_PAGES_DIR = Path(__file__).parent.parent / "pages"

# Commission knobs the page may set, with coercion.
_COMMISSION_TUNABLE = {"max_per_day": int, "auto_requeue": bool}


@routes.get("/settings")
async def settings_page(request: web.Request) -> web.Response:
    """The knobs page: playback, greeter, commissions, producer brains."""
    page = _PAGES_DIR / "settings.html"
    if not page.exists():
        raise web.HTTPNotFound(reason="Settings page missing from build")
    return web.Response(text=page.read_text(encoding="utf-8"), content_type="text/html")


@routes.get("/api/settings")
async def all_settings(request: web.Request) -> web.Response:
    """Every live setting in one read."""
    out: dict = {}

    mixer = request.app.get("mixer")
    if mixer is not None:
        try:
            out["playback"] = await mixer.get_volumes()
        except Exception:
            out["playback"] = {}

    greeter = request.app.get("greeter")
    if greeter is not None:
        out["greeter"] = greeter.settings()

    commissions = request.app.get("commissions")
    if commissions is not None:
        out["commissions"] = {
            "max_per_day": commissions.max_per_day,
            "auto_requeue": commissions.auto_requeue,
            "poll_interval": commissions.poll_interval,
            "owned_shows": sorted(commissions.owned_shows),
        }

    return web.json_response(out)


async def _persist(request: web.Request, section: str, applied: dict) -> None:
    store = request.app.get("config_store")
    if store is None:
        return
    for key, value in applied.items():
        await store.set(section, key, value)


@routes.put("/api/settings/greeter")
async def set_greeter(request: web.Request) -> web.Response:
    """Turn greeter knobs. Body: any subset of the greeter's TUNABLE keys."""
    greeter = request.app.get("greeter")
    if greeter is None:
        raise web.HTTPServiceUnavailable(reason="Greeter is not running")
    try:
        body = await request.json()
        applied = greeter.apply_settings(body)
    except web.HTTPException:
        raise
    except Exception as e:
        raise web.HTTPBadRequest(reason=f"Bad value: {e}")
    await _persist(request, "greeter", applied)
    return web.json_response({"ok": True, "applied": applied})


@routes.put("/api/settings/commissions")
async def set_commissions(request: web.Request) -> web.Response:
    """Turn commissioning knobs. Body: {"max_per_day": 3, "auto_requeue": true}."""
    commissions = request.app.get("commissions")
    if commissions is None:
        raise web.HTTPServiceUnavailable(reason="Commissioning is not enabled")
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Body must be JSON")

    applied = {}
    for key, kind in _COMMISSION_TUNABLE.items():
        if key not in body:
            continue
        value = body[key]
        if kind is bool and isinstance(value, str):
            value = value.lower() in ("1", "true", "yes", "on")
        try:
            value = kind(value)
        except Exception as e:
            raise web.HTTPBadRequest(reason=f"Bad value for {key}: {e}")
        if key == "max_per_day":
            value = max(1, value)
        setattr(commissions, key, value)
        applied[key] = value

    await _persist(request, "commissions", applied)
    return web.json_response({"ok": True, "applied": applied})
