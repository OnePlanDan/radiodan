"""
The player — Radio Dan's own front door.

VLC made the listener an anonymous integer in Icecast's count. This page plays
the same stream but *identifies* its listener (name + device, heartbeated to
the greeter), and wires the station's existing APIs to glass: star, skip,
requests, seed buttons, talk to the DJ, bulletin on demand, and summoning the
late-night show.

Design brief (gta-latest.json): primary way to listen, phone + desktop,
named-device identity, lean-back 30 — the page is a radio first and a control
surface second.
"""

import logging
import time
from datetime import datetime
from pathlib import Path

from aiohttp import web

from bridge.web.helpers import get_planner, get_service

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()

_PAGES_DIR = Path(__file__).parent.parent / "pages"

LATENIGHT_SHOW = "radiodan-latenight"


def _greeter(request: web.Request):
    greeter = request.app.get("greeter")
    if greeter is None:
        raise web.HTTPServiceUnavailable(reason="Greeter is not running")
    return greeter


@routes.get("/player")
async def player_page(request: web.Request) -> web.Response:
    """The station's own player: stream, identity, and every control."""
    page = _PAGES_DIR / "player.html"
    if not page.exists():
        raise web.HTTPNotFound(reason="Player page missing from build")
    return web.Response(text=page.read_text(encoding="utf-8"), content_type="text/html")


# =====================================================================
# IDENTITY
# =====================================================================

@routes.post("/api/presence")
async def presence_heartbeat(request: web.Request) -> web.Response:
    """The player identifies its listener. Body: {"name": "Dan", "device": "phone"}."""
    greeter = _greeter(request)
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Body must be JSON")
    name = (body.get("name") or "").strip()
    if not name:
        raise web.HTTPBadRequest(reason="name is required")
    await greeter.note_presence(name, (body.get("device") or "").strip())
    return web.json_response({"ok": True})


@routes.get("/api/presence")
async def presence_list(request: web.Request) -> web.Response:
    """Named listeners the player page has reported, most recent first."""
    greeter = _greeter(request)
    return web.json_response({"listeners": await greeter.known_listeners()})


# =====================================================================
# SUMMONING THE LATE-NIGHT SHOW
# =====================================================================

@routes.post("/api/player/summon")
async def summon_badsignal(request: web.Request) -> web.Response:
    """The ambush button: put Bad Signal on air now, or order it honestly."""
    commissions = request.app.get("commissions")
    if commissions is None:
        raise web.HTTPServiceUnavailable(reason="Commissioning is not enabled")

    ready = [r for r in await commissions.ready() if r["show"] == LATENIGHT_SHOW]
    if ready:
        row = ready[0]
        planner = get_planner(request)
        mixer = request.app["mixer"]
        stream_context = request.app["stream_context"]
        if not await planner.insert_item(commissions.to_queue_item(row), 0):
            raise web.HTTPInternalServerError(reason="Planner refused the episode")
        await mixer.next_track()
        await stream_context.notify_skip(source="system")
        await commissions.mark_aired(row["job_id"])
        logger.info(f"Bad Signal summoned to air: {row['title']}")
        return web.json_response({"status": "on_air", "title": row["title"]})

    pending = [r for r in await commissions.pending() if r["show"] == LATENIGHT_SHOW]
    if pending:
        eta = max(1, round((pending[0]["requested_at"] + 40 * 60 - time.time()) / 60))
        return web.json_response({
            "status": "building",
            "eta_minutes": eta,
            "note": "An episode is already in production — it can be summoned when it lands.",
        })

    now = datetime.now()
    midnight = datetime(now.year, now.month, now.day).timestamp()
    ordered_today = sum(
        1 for r in await commissions.recent(50) if r["requested_at"] >= midnight
    )
    cap = getattr(commissions, "max_per_day", 3)
    if ordered_today >= cap:
        return web.json_response({
            "status": "budget_spent",
            "note": f"The station already ordered {ordered_today} episodes today "
                    f"(cap {cap}). Duke and Nyx rest until tomorrow.",
        })

    concept = (
        f"AMBUSH EPISODE, summoned by the listener at "
        f"{now.strftime('%H:%M on %A')}. They pressed a button to drag Duke and "
        f"Nyx on air right now, and the hosts know it — open mid-complaint about "
        f"being summoned like a service. Two commercials, station ID, transmitter "
        f"forecast. A full ten minutes."
    )
    try:
        row = await commissions.commission(
            LATENIGHT_SHOW, concept, location="Gothenburg, Sweden"
        )
    except PermissionError as e:
        raise web.HTTPForbidden(reason=str(e))
    return web.json_response({
        "status": "ordered",
        "job_id": row["job_id"],
        "eta_minutes": 40,
        "note": "Summons heard. The episode is being written now (locally — "
                "give it ~40 minutes) and airs when it lands.",
    })


# =====================================================================
# VOICE IN
# =====================================================================

@routes.post("/api/player/voice")
async def voice_message(request: web.Request) -> web.Response:
    """Spoken message from the listener: transcribe, answer on air.

    Multipart with an `audio` file part (webm/ogg/wav from MediaRecorder).
    """
    stt_service = get_service(request, "stt_service")
    tts_service = get_service(request, "tts_service")
    llm_service = get_service(request, "llm_service")
    mixer = request.app["mixer"]
    stream_context = request.app["stream_context"]

    reader = await request.multipart()
    part = await reader.next()
    while part is not None and part.name != "audio":
        part = await reader.next()
    if part is None:
        raise web.HTTPBadRequest(reason="No audio part in upload")

    voice_dir = Path(__file__).parent.parent.parent.parent / "tmp" / "voice_in"
    voice_dir.mkdir(parents=True, exist_ok=True)
    audio_path = voice_dir / f"msg-{int(time.time())}.webm"
    with open(audio_path, "wb") as f:
        while True:
            chunk = await part.read_chunk()
            if not chunk:
                break
            f.write(chunk)

    try:
        message = (await stt_service.transcribe(audio_path)) or ""
    except Exception as e:
        raise web.HTTPBadGateway(reason=f"Transcription failed: {e}")
    finally:
        audio_path.unlink(missing_ok=True)
    message = message.strip()
    if not message:
        return web.json_response({"ok": False, "reason": "Heard nothing in that"})

    track = stream_context.current_track or {}
    context = f"Now playing: {track.get('artist', '?')} — {track.get('title', '?')}"
    enriched = (
        f"[Radio context]\n{context}\n\n[Listener spoke this aloud]\n{message}"
    )
    response_text = await llm_service.chat(enriched)
    audio = await tts_service.speak(response_text)
    await mixer.queue_tts(audio)

    return web.json_response({
        "ok": True, "heard": message, "response": response_text,
        "note": "Answered on air",
    })
