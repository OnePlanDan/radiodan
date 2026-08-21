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

import asyncio
import logging
from pathlib import Path

import aiohttp
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

    if request.query.get("probe") == "1":
        out["services"] = await _probe_services(request)

    return web.json_response(out)


def _service_catalog(request: web.Request) -> list[dict]:
    """Every external endpoint the station calls, deduplicated, with its role.

    This exists because "what are we calling?" should be one click, not a
    grep — the TTS move from Forge to Apollo was live for two days before
    anyone could see that the running process still held the old address.
    """
    ctx = request.app.get("ctx_kwargs", {})
    services: list[dict] = []
    seen: set[str] = set()

    def add(role: str, url: str | None):
        if not url or url in seen:
            return
        seen.add(url)
        services.append({"role": role, "url": url})

    tts = ctx.get("tts_service")
    if tts is not None:
        add("TTS — primary voice host", getattr(tts, "endpoint", None))
        for url in (getattr(tts, "voice_map", None) or {}).values():
            add("TTS — per-voice route", url)
        for chain in (getattr(tts, "fallbacks", None) or {}).values():
            for entry in chain if isinstance(chain, list) else []:
                if isinstance(entry, dict):
                    add("TTS — failover route", entry.get("endpoint"))
        default_fb = getattr(tts, "default_fallback", None) or {}
        add("TTS — catch-all fallback", default_fb.get("endpoint"))

    stt = ctx.get("stt_service")
    if stt is not None:
        add("STT — transcription", getattr(stt, "endpoint", None))

    llm = ctx.get("llm_service")
    if llm is not None:
        add("LLM — chat (Ollama)", getattr(llm, "endpoint", None))

    commissions = request.app.get("commissions")
    if commissions is not None:
        add("AudioSegment — episode production",
            getattr(getattr(commissions, "client", None), "base_url", None))

    tracker = request.app.get("listener_tracker")
    if tracker is not None:
        add("Icecast — the stream itself", getattr(tracker, "status_url", None))

    return services


async def _probe_services(request: web.Request) -> list[dict]:
    """Reachability per endpoint: any HTTP answer counts as alive."""
    services = _service_catalog(request)

    async def probe(entry: dict) -> dict:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    entry["url"], timeout=aiohttp.ClientTimeout(total=2.5)
                ) as resp:
                    return {**entry, "reachable": True, "http_status": resp.status}
        except Exception as e:
            return {**entry, "reachable": False, "error": type(e).__name__}

    probed = list(await asyncio.gather(*(probe(s) for s in services)))

    # Liquidsoap speaks telnet, not HTTP — ask the mixer instead.
    mixer = request.app.get("mixer")
    if mixer is not None:
        try:
            alive = await mixer.health_check()
        except Exception:
            alive = False
        probed.append({"role": "Liquidsoap — playout (telnet)",
                       "url": f"telnet {mixer.host}:{mixer.port}",
                       "reachable": bool(alive)})
    return probed


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
