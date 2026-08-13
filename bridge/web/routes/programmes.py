"""
Programme routes — commissioning episodes and putting them on air.

A programme episode is a block of material scheduled like a song. Ordering one
is asynchronous by nature: production takes ~20 minutes for a 7-minute episode,
so `POST /api/programmes` returns a job immediately and the audio is collected
in the background. Nothing here can be requested at air time.

Music is never at risk from any of this. A commission is speculative; if it is
late, fails, or never arrives, the queue carries on with songs.
"""

import logging

from aiohttp import web

from bridge.web.helpers import get_planner

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


def _service(request: web.Request):
    service = request.app.get("commissions")
    if service is None:
        raise web.HTTPServiceUnavailable(reason="Commissioning is not enabled")
    return service


def _as_dict(row) -> dict:
    return {k: row[k] for k in row.keys()}


@routes.get("/api/programmes")
async def list_programmes(request: web.Request) -> web.Response:
    """Recent commissions and their state. Query: limit."""
    service = _service(request)
    try:
        limit = max(1, min(100, int(request.query.get("limit", 20))))
    except ValueError:
        limit = 20
    rows = await service.recent(limit)
    return web.json_response({
        "commissions": [_as_dict(r) for r in rows],
        "ready": len(await service.ready()),
        "pending": len(await service.pending()),
    })


@routes.post("/api/programmes")
async def commission(request: web.Request) -> web.Response:
    """Commission an episode. Returns a job immediately; audio arrives later."""
    service = _service(request)
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Body must be JSON")

    show = (body.get("show") or "").strip()
    concept = (body.get("concept") or "").strip()
    if not show or not concept:
        raise web.HTTPBadRequest(reason="show and concept are required")

    try:
        row = await service.commission(
            show, concept,
            location=body.get("location") or None,
            weight=body.get("weight"),
            context_mode=body.get("context_mode"),
        )
    except Exception as e:
        logger.exception("Commission failed")
        raise web.HTTPBadGateway(reason=f"AudioSegment rejected the commission: {e}")

    return web.json_response({
        "ok": True,
        "commission": _as_dict(row),
        # Setting expectations honestly: measured history, not the optimistic
        # estimate the production service quotes.
        "note": "Production takes roughly 20 minutes for a 7 minute episode. "
                "Poll GET /api/programmes or wait for state 'ready'.",
    })


@routes.get("/api/programmes/shows")
async def shows(request: web.Request) -> web.Response:
    """Shows that can be commissioned from."""
    service = _service(request)
    try:
        return web.json_response({"shows": await service.client.shows()})
    except Exception as e:
        raise web.HTTPBadGateway(reason=f"Could not reach AudioSegment: {e}")


@routes.post("/api/programmes/{job_id}/air")
async def air(request: web.Request) -> web.Response:
    """Put a delivered episode into the queue. Body: {"position": N} (optional)."""
    service = _service(request)
    job_id = request.match_info["job_id"]

    row = await service.get(job_id)
    if row is None:
        raise web.HTTPNotFound(reason=f"No commission {job_id}")
    if row["state"] != "ready":
        raise web.HTTPConflict(
            reason=f"Commission is '{row['state']}', not ready to air"
        )

    position = None
    try:
        body = await request.json()
        if body.get("position") is not None:
            position = int(body["position"])
    except Exception:
        pass

    planner = get_planner(request)
    # Same call a song goes through — the episode is just another queue item.
    queued = await planner.insert_item(service.to_queue_item(row), position)
    if not queued:
        raise web.HTTPInternalServerError(reason="Planner refused the episode")

    await service.mark_aired(job_id)
    return web.json_response({
        "ok": True,
        "queued": _as_dict(await service.get(job_id)),
        "queue_length": len(planner.upcoming),
    })
