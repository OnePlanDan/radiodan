"""
Design briefs — forms the station serves to its owner, answers it keeps.

A design decision like "what should the late-night show be" is dozens of small
judgement calls: voices, names, sliders of taste, event triggers. Asking them
one chat message at a time loses half of them. So the station serves a form,
the owner fills in what he can, and the answers land as JSON in the station
directory where the agent picks them up.

Submissions are append-only (one timestamped file each) with a `-latest`
pointer, so a second pass at the form never destroys the first.
"""

import json
import logging
import os
import time
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()

_PAGES_DIR = Path(__file__).parent.parent / "pages"


def _design_dir() -> Path:
    station_dir = os.environ.get("RADIODAN_STATION_DIR", "stations/radio-dan")
    d = Path(station_dir) / "design"
    d.mkdir(parents=True, exist_ok=True)
    return d


@routes.get("/design/gta")
async def gta_form(request: web.Request) -> web.Response:
    """The Bad Signal After Dark design brief — a form the owner fills in."""
    page = _PAGES_DIR / "gta_design.html"
    if not page.exists():
        raise web.HTTPNotFound(reason="Form page missing from build")
    return web.Response(text=page.read_text(encoding="utf-8"), content_type="text/html")


@routes.post("/api/design/gta")
async def submit_gta(request: web.Request) -> web.Response:
    """Store a design-brief submission. Body: the form's answers as JSON."""
    try:
        answers = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="Body must be JSON")
    if not isinstance(answers, dict) or not answers:
        raise web.HTTPBadRequest(reason="Expected a non-empty JSON object")

    submitted_at = time.time()
    record = {"submitted_at": submitted_at, "answers": answers}
    d = _design_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(submitted_at))
    path = d / f"gta-{stamp}.json"
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    (d / "gta-latest.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info(f"GTA design brief received ({len(answers)} fields) -> {path.name}")
    return web.json_response({"ok": True, "stored": path.name, "fields": len(answers)})


@routes.get("/api/design/gta")
async def latest_gta(request: web.Request) -> web.Response:
    """The most recent design-brief submission (used to prefill the form)."""
    latest = _design_dir() / "gta-latest.json"
    if not latest.exists():
        return web.json_response({"answers": None})
    try:
        return web.json_response(json.loads(latest.read_text(encoding="utf-8")))
    except Exception:
        return web.json_response({"answers": None})
