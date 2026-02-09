"""
Audio controls routes — volume, mute, duck, skip.
"""

import aiohttp_jinja2
from aiohttp import web

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
