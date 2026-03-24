"""
Audio controls routes — volume, mute, duck, skip, say, chat.
"""

import logging
from datetime import datetime
from html import escape

import aiohttp_jinja2
from aiohttp import web

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


@routes.get("/audio")
@aiohttp_jinja2.template("audio.html")
async def audio_page(request: web.Request) -> dict:
    """Render the audio controls page."""
    mixer = request.app["mixer"]

    volumes = await _get_volumes(mixer)

    return {
        "page": "audio",
        **{k: volumes[k] for k in volumes},
        "music_muted": mixer.music_muted,
        "tts_muted": mixer.tts_muted,
        "random_mode": mixer.random_mode,
    }


async def _get_volumes(mixer):
    """Get volumes with fallback defaults."""
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


@routes.get("/audio/state")
async def audio_state(request: web.Request) -> web.Response:
    """Return current audio state as HTMX partial."""
    mixer = request.app["mixer"]
    volumes = await _get_volumes(mixer)

    response = aiohttp_jinja2.render_template(
        "audio.html",
        request,
        {
            "page": "audio",
            **{k: volumes[k] for k in volumes},
            "music_muted": mixer.music_muted,
            "tts_muted": mixer.tts_muted,
            "random_mode": mixer.random_mode,
        },
    )
    return response


@routes.post("/audio/volume")
async def set_volume(request: web.Request) -> web.Response:
    """Set music volume via HTMX."""
    mixer = request.app["mixer"]
    data = await request.post()
    try:
        value = float(data.get("value", 1.0))
    except (ValueError, TypeError):
        value = 1.0
    await mixer.set_music_volume(value)
    return web.Response(text=f"{int(value * 100)}%", content_type="text/html")


@routes.post("/audio/tts-volume")
async def set_tts_volume(request: web.Request) -> web.Response:
    """Set TTS volume via HTMX."""
    mixer = request.app["mixer"]
    data = await request.post()
    try:
        value = float(data.get("value", 0.85))
    except (ValueError, TypeError):
        value = 0.85
    await mixer.set_tts_volume(value)
    return web.Response(text=f"{int(value * 100)}%", content_type="text/html")


@routes.post("/audio/earcon-volume")
async def set_earcon_volume(request: web.Request) -> web.Response:
    """Set earcon volume via HTMX."""
    mixer = request.app["mixer"]
    data = await request.post()
    try:
        value = float(data.get("value", 0.5))
    except (ValueError, TypeError):
        value = 0.5
    await mixer.set_earcon_volume(value)
    return web.Response(text=f"{int(value * 100)}%", content_type="text/html")


@routes.post("/audio/duck")
async def set_duck(request: web.Request) -> web.Response:
    """Set duck amount via HTMX."""
    mixer = request.app["mixer"]
    data = await request.post()
    try:
        value = float(data.get("value", 0.15))
    except (ValueError, TypeError):
        value = 0.15
    await mixer.set_duck_amount(value)
    return web.Response(text=f"{int(value * 100)}%", content_type="text/html")


@routes.post("/audio/crossfade")
async def set_crossfade(request: web.Request) -> web.Response:
    """Set crossfade duration via HTMX."""
    mixer = request.app["mixer"]
    data = await request.post()
    try:
        value = float(data.get("value", 5.0))
    except (ValueError, TypeError):
        value = 5.0
    await mixer.set_crossfade_duration(value)
    return web.Response(text=f"{value:.1f}s", content_type="text/html")


@routes.post("/audio/duck-in-duration")
async def set_duck_in_duration(request: web.Request) -> web.Response:
    """Set duck-in duration via HTMX."""
    mixer = request.app["mixer"]
    data = await request.post()
    try:
        value = float(data.get("value", 0.8))
    except (ValueError, TypeError):
        value = 0.8
    await mixer.set_duck_in_duration(value)
    return web.Response(text=f"{value:.2f}s", content_type="text/html")


@routes.post("/audio/duck-out-duration")
async def set_duck_out_duration(request: web.Request) -> web.Response:
    """Set duck-out duration via HTMX."""
    mixer = request.app["mixer"]
    data = await request.post()
    try:
        value = float(data.get("value", 0.6))
    except (ValueError, TypeError):
        value = 0.6
    await mixer.set_duck_out_duration(value)
    return web.Response(text=f"{value:.2f}s", content_type="text/html")


@routes.post("/audio/duck-in-curve")
async def set_duck_in_curve(request: web.Request) -> web.Response:
    """Set duck-in bezier curve via HTMX."""
    mixer = request.app["mixer"]
    data = await request.post()
    try:
        value = float(data.get("value", 0.7))
    except (ValueError, TypeError):
        value = 0.7
    await mixer.set_duck_in_curve(value)
    return web.Response(text=f"{value:.2f}", content_type="text/html")


@routes.post("/audio/duck-out-curve")
async def set_duck_out_curve(request: web.Request) -> web.Response:
    """Set duck-out bezier curve via HTMX."""
    mixer = request.app["mixer"]
    data = await request.post()
    try:
        value = float(data.get("value", 0.3))
    except (ValueError, TypeError):
        value = 0.3
    await mixer.set_duck_out_curve(value)
    return web.Response(text=f"{value:.2f}", content_type="text/html")


@routes.post("/audio/music-mute")
async def toggle_music_mute(request: web.Request) -> web.Response:
    """Toggle music mute via HTMX."""
    mixer = request.app["mixer"]
    is_muted, vol = await mixer.toggle_music_mute()
    label = "Unmute" if is_muted else "Mute"
    css = "btn-danger" if is_muted else "btn-secondary"
    return web.Response(
        text=f'<button class="btn {css}" hx-post="/audio/music-mute" hx-swap="outerHTML">{label}</button>',
        content_type="text/html",
    )


@routes.post("/audio/tts-mute")
async def toggle_tts_mute(request: web.Request) -> web.Response:
    """Toggle TTS mute via HTMX."""
    mixer = request.app["mixer"]
    is_muted, vol = await mixer.toggle_tts_mute()
    label = "Unmute" if is_muted else "Mute"
    css = "btn-danger" if is_muted else "btn-secondary"
    return web.Response(
        text=f'<button class="btn {css}" hx-post="/audio/tts-mute" hx-swap="outerHTML">{label}</button>',
        content_type="text/html",
    )


@routes.post("/audio/skip")
async def skip_track(request: web.Request) -> web.Response:
    """Skip to next track via HTMX."""
    mixer = request.app["mixer"]
    await mixer.next_track()
    stream_context = request.app["stream_context"]
    await stream_context.notify_skip()
    return web.Response(text='<span class="flash success">Skipped!</span>', content_type="text/html")


@routes.post("/audio/star")
async def star_track(request: web.Request) -> web.Response:
    """Star/like the current track via HTMX."""
    from bridge.booth import booth

    stream_context = request.app["stream_context"]
    planner = request.app["ctx_kwargs"]["playlist_planner"]

    track = stream_context.current_track or {}
    filename = track.get("filename", "")
    if not filename:
        return web.Response(
            text='<span class="flash error">No track playing</span>',
            content_type="text/html",
        )

    file_path = planner.resolve_file_path(filename)
    await planner.star_track(file_path)
    booth.track_star(track.get("artist", "Unknown"), track.get("title", "Unknown"))

    html = (
        '<button class="star-btn starred"'
        ' hx-post="/audio/unstar"'
        ' hx-target="#star-btn"'
        ' hx-swap="innerHTML">'
        '\u2605 Starred</button>'
    )
    return web.Response(text=html, content_type="text/html")


@routes.post("/audio/unstar")
async def unstar_track(request: web.Request) -> web.Response:
    """Remove star/like from the current track via HTMX."""
    stream_context = request.app["stream_context"]
    planner = request.app["ctx_kwargs"]["playlist_planner"]

    track = stream_context.current_track or {}
    filename = track.get("filename", "")
    if not filename:
        return web.Response(
            text='<span class="flash error">No track playing</span>',
            content_type="text/html",
        )

    file_path = planner.resolve_file_path(filename)
    await planner.unstar_track(file_path)

    html = (
        '<button class="star-btn"'
        ' hx-post="/audio/star"'
        ' hx-target="#star-btn"'
        ' hx-swap="innerHTML">'
        '\u2606 Star</button>'
    )
    return web.Response(text=html, content_type="text/html")


# ═══════════════════════════════════════════════════════════════════════════════
# Say / Time / LLM Chat
# ═══════════════════════════════════════════════════════════════════════════════


@routes.post("/audio/say")
async def say_text(request: web.Request) -> web.Response:
    """Speak text on the stream via TTS."""
    from bridge.booth import booth

    tts_service = request.app["ctx_kwargs"]["tts_service"]
    mixer = request.app["mixer"]
    data = await request.post()
    text = (data.get("text") or "").strip()
    if not text:
        return web.Response(
            text='<span class="flash error">No text provided</span>',
            content_type="text/html",
        )
    try:
        audio_path = await tts_service.speak(text)
        success = await mixer.queue_tts(audio_path)
        if success:
            booth.tts_request(text[:80], "web")
            return web.Response(
                text=f'<span class="flash success">Speaking: {escape(text[:80])}</span>',
                content_type="text/html",
            )
        return web.Response(
            text='<span class="flash error">Failed to queue audio</span>',
            content_type="text/html",
        )
    except Exception as e:
        logger.exception("TTS error in /audio/say")
        return web.Response(
            text=f'<span class="flash error">TTS error: {escape(str(e)[:100])}</span>',
            content_type="text/html",
        )


@routes.post("/audio/time-announce")
async def time_announce(request: web.Request) -> web.Response:
    """Announce the current time on the stream via TTS."""
    tts_service = request.app["ctx_kwargs"]["tts_service"]
    mixer = request.app["mixer"]
    now = datetime.now()
    time_text = f"The time is {now.strftime('%H:%M')}"
    try:
        audio_path = await tts_service.speak(time_text)
        await mixer.queue_tts(audio_path)
        return web.Response(
            text=f'<span class="flash success">Announced: {now.strftime("%H:%M:%S")}</span>',
            content_type="text/html",
        )
    except Exception as e:
        logger.exception("TTS error in /audio/time-announce")
        return web.Response(
            text=f'<span class="flash error">TTS error: {escape(str(e)[:100])}</span>',
            content_type="text/html",
        )


@routes.post("/audio/chat")
async def llm_chat(request: web.Request) -> web.Response:
    """Send text to LLM, speak the response on the stream."""
    from bridge.booth import booth

    tts_service = request.app["ctx_kwargs"]["tts_service"]
    llm_service = request.app["ctx_kwargs"]["llm_service"]
    mixer = request.app["mixer"]
    data = await request.post()
    message = (data.get("message") or "").strip()
    if not message:
        return web.Response(
            text='<div class="chat-msg error">No message provided</div>',
            content_type="text/html",
        )
    try:
        response_text = await llm_service.chat(message)
        audio_path = await tts_service.speak(response_text)
        await mixer.queue_tts(audio_path)
        booth.tts_request(f"LLM chat: {message[:50]}", "web")
        html = (
            f'<div class="chat-msg user">{escape(message)}</div>'
            f'<div class="chat-msg assistant">{escape(response_text)}</div>'
            f'<div class="flash success" style="margin-top:0.4rem;font-size:0.8rem">Speaking on stream</div>'
        )
        return web.Response(text=html, content_type="text/html")
    except Exception as e:
        logger.exception("Error in /audio/chat")
        return web.Response(
            text=f'<div class="chat-msg user">{escape(message)}</div>'
                 f'<div class="chat-msg error">Error: {escape(str(e)[:100])}</div>',
            content_type="text/html",
        )
