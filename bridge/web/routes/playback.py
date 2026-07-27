"""
Playback routes — volume, skip, mute, duck, star, say, chat.
"""

import json
import logging
from datetime import datetime

from aiohttp import web

from bridge.web.helpers import get_planner, get_service

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


async def _get_volumes(mixer) -> dict:
    defaults = {
        "music_vol": 1.0, "tts_vol": 1.0, "earcon_vol": 0.5,
        "duck_amount": 0.15, "crossfade_duration": 5.0,
        "duck_in_duration": 0.8, "duck_out_duration": 0.6,
        "duck_in_curve": 0.7, "duck_out_curve": 0.3,
    }
    try:
        return await mixer.get_volumes()
    except Exception:
        return defaults


@routes.get("/api/playback")
async def get_playback(request: web.Request) -> web.Response:
    """All audio state: volumes, mute states."""
    mixer = request.app["mixer"]
    volumes = await _get_volumes(mixer)
    return web.json_response({
        **volumes,
        "music_muted": mixer.music_muted,
        "tts_muted": mixer.tts_muted,
    })


@routes.put("/api/playback/volume")
async def set_volume(request: web.Request) -> web.Response:
    """Set volume. Body: {"target": "music|tts|earcon", "value": 0.8}"""
    mixer = request.app["mixer"]
    body = await request.json()
    target = body.get("target", "music")
    value = float(body.get("value", 1.0))

    setters = {
        "music": mixer.set_music_volume,
        "tts": mixer.set_tts_volume,
        "earcon": mixer.set_earcon_volume,
    }
    setter = setters.get(target)
    if not setter:
        raise web.HTTPBadRequest(reason=f"Invalid target: {target}. Use music|tts|earcon")

    await setter(value)
    return web.json_response({"ok": True, "target": target, "value": value})


@routes.put("/api/playback/duck")
async def set_duck(request: web.Request) -> web.Response:
    """Set duck parameters. Body: {"amount": 0.15, "in_duration": 0.8, ...}"""
    mixer = request.app["mixer"]
    body = await request.json()

    if "amount" in body:
        await mixer.set_duck_amount(float(body["amount"]))
    if "in_duration" in body:
        await mixer.set_duck_in_duration(float(body["in_duration"]))
    if "out_duration" in body:
        await mixer.set_duck_out_duration(float(body["out_duration"]))
    if "in_curve" in body:
        await mixer.set_duck_in_curve(float(body["in_curve"]))
    if "out_curve" in body:
        await mixer.set_duck_out_curve(float(body["out_curve"]))

    return web.json_response({"ok": True})


@routes.put("/api/playback/crossfade")
async def set_crossfade(request: web.Request) -> web.Response:
    """Set crossfade duration. Body: {"duration": 5.0}"""
    mixer = request.app["mixer"]
    body = await request.json()
    duration = float(body.get("duration", 5.0))
    await mixer.set_crossfade_duration(duration)
    return web.json_response({"ok": True, "duration": duration})


@routes.post("/api/playback/skip")
async def skip(request: web.Request) -> web.Response:
    """Skip the current track."""
    mixer = request.app["mixer"]
    stream_context = request.app["stream_context"]
    await mixer.next_track()
    await stream_context.notify_skip()
    return web.json_response({"ok": True})


@routes.post("/api/playback/mute")
async def toggle_mute(request: web.Request) -> web.Response:
    """Toggle mute. Body: {"target": "music|tts"}"""
    mixer = request.app["mixer"]
    body = await request.json()
    target = body.get("target", "music")

    if target == "music":
        is_muted, vol = await mixer.toggle_music_mute()
    elif target == "tts":
        is_muted, vol = await mixer.toggle_tts_mute()
    else:
        raise web.HTTPBadRequest(reason=f"Invalid target: {target}. Use music|tts")

    return web.json_response({"ok": True, "target": target, "muted": is_muted, "volume": vol})


@routes.post("/api/playback/star")
async def star_track(request: web.Request) -> web.Response:
    """Star the current track. Each call adds one star (counter, not toggle)."""
    from bridge.booth import booth

    stream_context = request.app["stream_context"]
    planner = get_planner(request)

    track = stream_context.current_track or {}
    filename = track.get("filename", "")
    if not filename:
        raise web.HTTPBadRequest(reason="No track playing")

    file_path = planner.resolve_file_path(filename)
    count = await planner.star_track(file_path)
    booth.track_star(track.get("artist", "Unknown"), track.get("title", "Unknown"))

    return web.json_response({
        "ok": True,
        "stars": count,
        "artist": track.get("artist", ""),
        "title": track.get("title", ""),
    })


@routes.delete("/api/playback/star")
async def unstar_track(request: web.Request) -> web.Response:
    """Remove all stars from the current track."""
    stream_context = request.app["stream_context"]
    planner = get_planner(request)

    track = stream_context.current_track or {}
    filename = track.get("filename", "")
    if not filename:
        raise web.HTTPBadRequest(reason="No track playing")

    file_path = planner.resolve_file_path(filename)
    await planner.unstar_track(file_path)

    return web.json_response({"ok": True, "stars": 0})


@routes.post("/api/playback/say")
async def say_text(request: web.Request) -> web.Response:
    """Speak text on the stream.

    Body: {"text": "Hello!", "speaker": "Eric", "instruct": "Speak like a pirate"}
    speaker and instruct are optional — defaults to global TTS config.
    """
    from bridge.booth import booth

    tts_service = get_service(request, "tts_service")
    mixer = request.app["mixer"]
    body = await request.json()
    text = (body.get("text") or "").strip()

    if not text:
        raise web.HTTPBadRequest(reason="No text provided")

    speaker = (body.get("speaker") or "").strip() or None
    instruct = (body.get("instruct") or "").strip() or None

    audio_path = await tts_service.speak(text, speaker=speaker, instruct=instruct)
    success = await mixer.queue_tts(audio_path)
    if not success:
        raise web.HTTPServiceUnavailable(reason="Failed to queue audio")

    booth.tts_request(text[:80], "api")
    return web.json_response({
        "ok": True,
        "text": text[:80],
        "speaker": speaker or tts_service.speaker,
        "instruct": instruct or tts_service.instruct,
    })


@routes.post("/api/playback/chat")
async def llm_chat(request: web.Request) -> web.Response:
    """Send text to LLM with playback context, speak the response.

    Body: {"message": "...", "speaker": "Eric", "instruct": "..."}
    speaker and instruct are optional.
    """
    from bridge.booth import booth

    tts_service = get_service(request, "tts_service")
    llm_service = get_service(request, "llm_service")
    mixer = request.app["mixer"]
    stream_context = request.app["stream_context"]
    body = await request.json()
    message = (body.get("message") or "").strip()

    if not message:
        raise web.HTTPBadRequest(reason="No message provided")

    # Build playback context for the LLM
    track = stream_context.current_track or {}
    now = datetime.now()
    context_parts = [f"Date: {now.strftime('%A %B %d, %Y')}", f"Time: {now.strftime('%H:%M')}"]
    if track.get("artist") or track.get("title"):
        context_parts.append(f"Now playing: {track.get('artist', 'Unknown')} — {track.get('title', 'Unknown')}")
        if track.get("album"):
            context_parts.append(f"Album: {track['album']}")
        if track.get("genre"):
            context_parts.append(f"Genre: {track['genre']}")
        if track.get("year"):
            context_parts.append(f"Year: {track['year']}")

    try:
        planner = get_planner(request)
        upcoming = planner.upcoming[:3]
        if upcoming:
            next_tracks = ", ".join(
                f"{t.get('artist', '?')} — {t.get('title', '?')}" for t in upcoming
            )
            context_parts.append(f"Coming up: {next_tracks}")
    except Exception:
        pass

    # Prepend context to the user message
    if context_parts:
        context_block = "\n".join(context_parts)
        enriched_message = f"[Radio context]\n{context_block}\n\n[Listener says]\n{message}"
    else:
        enriched_message = message

    speaker = (body.get("speaker") or "").strip() or None
    instruct = (body.get("instruct") or "").strip() or None

    response_text = await llm_service.chat(enriched_message)
    audio_path = await tts_service.speak(response_text, speaker=speaker, instruct=instruct)
    await mixer.queue_tts(audio_path)
    booth.tts_request(f"LLM chat: {message[:50]}", "api")

    return web.json_response({
        "ok": True,
        "message": message,
        "context": context_parts,
        "response": response_text,
        "speaker": speaker or tts_service.speaker,
    })
