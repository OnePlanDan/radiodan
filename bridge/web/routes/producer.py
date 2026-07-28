"""Producer API routes — external control surface for the producer plugin."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


_ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp"}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB

# How long POST /seed waits for the seed to actually go live before answering
# "still pending". Seeds queue behind the producer's signal loop, and a phase-2
# build can hold one for a minute, so this is generous by default and callers
# who don't want to block can pass ?wait=0.
_SEED_WAIT_SECONDS = 20.0
_SEED_WAIT_MAX = 120.0


def _seed_wait_seconds(request: web.Request) -> float:
    """Read ?wait=<seconds>, clamped. Invalid values fall back to the default."""
    raw = request.query.get("wait")
    if raw is None:
        return _SEED_WAIT_SECONDS
    try:
        return max(0.0, min(_SEED_WAIT_MAX, float(raw)))
    except ValueError:
        return _SEED_WAIT_SECONDS


def _get_producer(request: web.Request):
    """Find the running producer plugin instance."""
    for p in request.app.get("plugins", []):
        if p.name == "producer" and p._running:
            return p
    return None


def _upload_dir() -> Path:
    station_env = os.environ.get("RADIODAN_STATION_DIR")
    base = Path(station_env) if station_env else Path("/tmp/radiodan_seeds")
    out = base / "uploads" / "seeds"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _ext_for(content_type: str, filename: str | None) -> str:
    ct = (content_type or "").lower()
    if "png" in ct:
        return ".png"
    if "webp" in ct:
        return ".webp"
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    if filename:
        low = filename.lower()
        for e in (".png", ".jpg", ".jpeg", ".webp"):
            if low.endswith(e):
                return e if e != ".jpeg" else ".jpg"
    return ".bin"


@routes.get("/api/producer/status")
async def producer_status(request: web.Request) -> web.Response:
    producer = _get_producer(request)
    if not producer:
        return web.json_response({"active": False})
    return web.json_response({"active": True, **producer.status})


@routes.get("/api/producer/characters")
async def producer_characters(request: web.Request) -> web.Response:
    producer = _get_producer(request)
    if not producer:
        return web.json_response({"characters": []})
    active_cast = set(producer._seed.cast) if producer._seed else set()
    chars = []
    for cid, c in producer._characters.items():
        chars.append({
            "id": cid,
            "name": c.name,
            "voice_speaker": c.voice_speaker,
            "genre_weights": c.genre_weights,
            "active": cid in active_cast,
        })
    return web.json_response({"characters": chars})


@routes.get("/api/producer/plan")
async def producer_plan(request: web.Request) -> web.Response:
    producer = _get_producer(request)
    if not producer:
        return web.json_response({"segments": []})
    return web.json_response({"segments": producer.plan_detail})


# =========================================================================
# POST /seed — the main entry point
# =========================================================================


@routes.post("/api/producer/seed")
async def producer_seed(request: web.Request) -> web.Response:
    """Set a new seed. Accepts JSON or multipart/form-data.

    Body fields (all optional; first present wins per priority order):
        text, song, character, cast[], genre, image_url
    Multipart also accepts:
        image (file upload — saved by sha256)
    Query:
        wait — seconds to wait for the seed to go live (default 20, max 120,
               0 to return immediately)

    Response:
        applied — the submitted seed is now the live seed
        pending — accepted but still queued behind the producer's signal loop;
                  poll /api/producer/status
        error   — set when the seed was rejected (interpretation failed)
        seed    — the seed actually in effect right now, never an unapplied one
    """
    producer = _get_producer(request)
    if not producer:
        raise web.HTTPServiceUnavailable(reason="Producer not active")

    content_type = (request.headers.get("Content-Type") or "").lower()
    body: dict = {}

    if content_type.startswith("multipart/"):
        reader = await request.multipart()
        async for part in reader:
            name = part.name
            if not name:
                continue
            if name == "image":
                ct = part.headers.get("Content-Type", "").lower()
                if ct and ct.split(";")[0] not in _ALLOWED_IMAGE_TYPES:
                    raise web.HTTPUnsupportedMediaType(reason=f"Image type {ct} not allowed")
                raw = await part.read(decode=False)
                if len(raw) > _MAX_IMAGE_BYTES:
                    raise web.HTTPRequestEntityTooLarge(
                        max_size=_MAX_IMAGE_BYTES, actual_size=len(raw),
                    )
                digest = hashlib.sha256(raw).hexdigest()[:16]
                ext = _ext_for(ct, part.filename)
                out = _upload_dir() / f"{digest}{ext}"
                out.write_bytes(raw)
                body["_uploaded_image_path"] = str(out)
            elif name == "cast":
                val = (await part.text()).strip()
                body.setdefault("cast", []).append(val) if val else None
            else:
                body[name] = (await part.text()).strip()
    else:
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(reason="Body must be JSON or multipart/form-data")
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(reason="JSON body must be an object")

    if not any(body.get(k) for k in ("text", "song", "song_path", "character", "cast", "genre", "image_url", "_uploaded_image_path")):
        raise web.HTTPBadRequest(
            reason="Seed must include one of: text, song, character, cast, genre, image, image_url"
        )

    ack = await producer.submit_seed(body)

    # Wait for the seed to actually go live rather than for producer._seed to be
    # non-null — it already is, which is why this used to return 200 alongside
    # the *previous* seed while the new one was still queued.
    started = time.monotonic()
    outcome: dict | None = None
    wait_seconds = _seed_wait_seconds(request)
    if wait_seconds > 0:
        try:
            # shield() so a timeout here doesn't cancel the producer's ack.
            outcome = await asyncio.wait_for(asyncio.shield(ack), timeout=wait_seconds)
        except asyncio.TimeoutError:
            outcome = None
    waited = round(time.monotonic() - started, 2)

    applied = bool(outcome and outcome["applied"])
    error = (outcome or {}).get("error", "")
    pending = outcome is None

    if pending:
        logger.info(
            f"Seed still queued after {waited}s — answering pending "
            "(producer busy; poll /api/producer/status)"
        )
    elif not applied:
        logger.warning(f"Seed rejected: {error}")

    return web.json_response({
        # False only when the seed was actively rejected; a still-queued seed was
        # accepted and will land.
        "ok": applied or pending,
        "applied": applied,
        "pending": pending,
        "error": error,
        "waited_seconds": waited,
        # Always the live seed, so this never claims a seed that isn't in effect.
        "seed": producer._seed.as_dict() if producer._seed else None,
        "script_building": producer._building,
    })


@routes.post("/api/producer/switch")
async def producer_switch(request: web.Request) -> web.Response:
    """Back-compat shortcut: POST {"character": "id"} routes through seed."""
    producer = _get_producer(request)
    if not producer:
        raise web.HTTPServiceUnavailable(reason="Producer not active")
    body = await request.json()
    char_id = body.get("character", "")
    if not char_id or char_id not in producer._characters:
        raise web.HTTPNotFound(reason=f"Unknown character: {char_id!r}")

    ack = await producer.submit_seed({"character": char_id})
    outcome: dict | None = None
    wait_seconds = _seed_wait_seconds(request)
    if wait_seconds > 0:
        try:
            outcome = await asyncio.wait_for(asyncio.shield(ack), timeout=wait_seconds)
        except asyncio.TimeoutError:
            outcome = None

    applied = bool(outcome and outcome["applied"])
    pending = outcome is None
    return web.json_response({
        "ok": applied or pending,
        "applied": applied,
        "pending": pending,
        "error": (outcome or {}).get("error", ""),
        "character": char_id,
    })


@routes.post("/api/producer/skip")
async def producer_skip(request: web.Request) -> web.Response:
    """Skip current track + trigger producer reaction."""
    producer = _get_producer(request)
    if not producer:
        raise web.HTTPServiceUnavailable(reason="Producer not active")
    mixer = request.app.get("mixer")
    if mixer:
        await mixer.next_track()
    return web.json_response({"ok": True})


@routes.post("/api/producer/quickrun")
async def producer_quickrun(request: web.Request) -> web.Response:
    """Inject a quick segment. Body: {"topic": "weather"}"""
    producer = _get_producer(request)
    if not producer:
        raise web.HTTPServiceUnavailable(reason="Producer not active")
    body = await request.json()
    topic = body.get("topic", "weather")
    await producer.request_quickrun(topic)
    return web.json_response({"ok": True, "topic": topic})


@routes.post("/api/producer/mood")
async def producer_mood(request: web.Request) -> web.Response:
    """Adjust genre weights live. Body: {"genre_weights": {"jazz": 8, "electronic": 2}}"""
    producer = _get_producer(request)
    if not producer:
        raise web.HTTPServiceUnavailable(reason="Producer not active")
    body = await request.json()
    weights = body.get("genre_weights", {})
    if not weights:
        raise web.HTTPBadRequest(reason="genre_weights required")
    await producer.adjust_mood(weights)
    return web.json_response({"ok": True})


# =========================================================================
# PUT /models — runtime backend swap
# =========================================================================


@routes.put("/api/producer/models")
async def producer_models(request: web.Request) -> web.Response:
    """Swap LLM backends at runtime. Body shape:

        {"interpreter": {"backend": "claude_cli", "model": "haiku"},
         "script_generator": {"backend": "ollama", "model": "gpt-oss:20b"},
         "vision": {"backend": "ollama", "model": "gemma3:27b"}}

    Only roles present in the body are updated. Ephemeral — not persisted.
    """
    producer = _get_producer(request)
    if not producer:
        raise web.HTTPServiceUnavailable(reason="Producer not active")
    body = await request.json()
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(reason="Body must be a JSON object")
    active = producer.update_models(body)
    return web.json_response({"ok": True, "models": active})
