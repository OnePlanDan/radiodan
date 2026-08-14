"""
API index — GET /api/, GET /api, GET /

Discovery for agents. Before this, `/`, `/api` and `/api/` all returned 404, so
anything arriving without a hand-written list had no way to find out what the
station could do — visible in the access log as an `agentos-probe` hitting `/`
and getting a 404.

The endpoint list is generated from aiohttp's live route table rather than
maintained by hand. `doc/producer-api-handoff.md` was the hand-written version and
it drifted out of date within weeks: it still described voice routing and a
character roster that had changed. A generated index cannot drift.
"""

import inspect
import logging
import time

from aiohttp import web

routes = web.RouteTableDef()

logger = logging.getLogger(__name__)

# Paths that answer with this index. `/` is included because it is the first
# thing anything probes.
_INDEX_PATHS = ("/", "/api", "/api/")

# Request-body shapes worth knowing at discovery time, so an agent gets oriented
# in one call instead of needing a second schema endpoint. Only for endpoints
# whose body is not obvious from the path.
_BODY_FIELDS: dict[str, dict] = {
    "POST /api/producer/seed": {
        "fields": {
            "text": "free text; an interpreter LLM classifies it and picks a host",
            "song": "seed from a specific track",
            "character": "host id — one of /api/producer/characters",
            "cast": "list of host ids for a multi-host show",
            "genre": "genre name; synonym-expanded (hip-hop also matches rap)",
            "image_url": "seed from an image (vision model)",
            "strict": "bool; hard-filter the library to the genre (default true for genre seeds)",
            "hard": "bool; skip the current track once the new first song is queued (one-shot)",
        },
        "query": {"wait": "seconds to wait for the seed to go live (default 20, max 120, 0 = return at once)"},
        "notes": "First non-empty field wins, in the order listed. Response reports "
                 "applied/pending/error and the seed actually in effect.",
    },
    "POST /api/programmes": {
        "fields": {
            "show": "show name from GET /api/programmes/shows (required)",
            "concept": "one or two sentences — the brief (required)",
            "location": "place name, for weather and real-world context",
            "weight": "1-10 emotional heaviness",
            "context_mode": "'sharp' (today's real events) or 'fuzzy' (ambient)",
        },
        "notes": "Asynchronous: returns a job immediately, audio lands ~20 min "
                 "later for a 7 min episode. Long blocks are far cheaper per "
                 "minute than short ones. Music is unaffected if it never arrives.",
    },
    "POST /api/playback/say": {
        "fields": {
            "text": "what to speak (required)",
            "speaker": "voice name; defaults to the station voice",
            "instruct": "voice style hint",
        },
        "notes": "Speaks immediately over the current track, ducking the music.",
    },
    "POST /api/queue": {
        "fields": {
            "file_path": "path of a track in the library (required)",
            "position": "queue index; appends when omitted",
        },
    },
    "GET /api/events/history": {
        "query": {
            "minutes": "how far back to look (default 60, max 1440)",
            "limit": "max events returned, newest first (default 200, max 1000)",
            "lane": "repeatable lane filter, e.g. lane=producer&lane=time",
        },
    },
    "POST /api/greeter/test": {
        "fields": {
            "first_of_day": "bool; also run the day's-first flow (breaks the song, airs the bulletin)",
        },
        "notes": "Fires a real greeting on air, ignoring the arrival cooldown.",
    },
}


def _summary(handler) -> str:
    """First line of the handler's docstring."""
    doc = inspect.getdoc(handler)
    if not doc:
        return ""
    return doc.strip().splitlines()[0].strip()


def _collect_endpoints(app: web.Application) -> list[dict]:
    """Read the live route table. Never hand-maintained, so it cannot go stale."""
    seen: set[tuple[str, str]] = set()
    found: list[dict] = []

    for route in app.router.routes():
        method = route.method.upper()
        # aiohttp registers HEAD alongside every GET; OPTIONS is handled by the
        # CORS middleware rather than a route. Neither is interesting here.
        if method in ("HEAD", "OPTIONS"):
            continue
        resource = route.resource
        path = getattr(resource, "canonical", None)
        if not path:
            continue
        key = (method, path)
        if key in seen:
            continue
        seen.add(key)

        entry = {"method": method, "path": path, "summary": _summary(route.handler)}
        entry.update(_BODY_FIELDS.get(f"{method} {path}", {}))
        found.append(entry)

    found.sort(key=lambda e: (e["path"], e["method"]))
    return found


@routes.get("/")
@routes.get("/api")
@routes.get("/api/")
async def api_index(request: web.Request) -> web.Response:
    """This index: what the station is and every endpoint it serves."""
    app = request.app
    start = app.get("start_time", time.time())

    station: dict = {
        "name": app.get("station_name", "Radio Dan"),
        "stream_url": app.get("stream_url", ""),
        "uptime_seconds": round(time.time() - start, 1),
    }

    # A pointer rather than a copy: the agent should learn where state lives.
    where = {
        "now_playing": "GET /api/status",
        "alerts": "GET /api/status (alerts[])",
        "service_health": "GET /api/status/health",
        "what_aired": "GET /api/events/history",
        "live_updates": "GET /api/events (server-sent events)",
        "show_state": "GET /api/producer/status",
        "upcoming": "GET /api/producer/plan",
        "hosts": "GET /api/producer/characters",
        "change_the_show": "POST /api/producer/seed",
        "speak_now": "POST /api/playback/say",
        "who_is_listening": "GET /api/listeners",
        "programmes": "GET /api/programmes",
        "commission_an_episode": "POST /api/programmes",
        "put_an_episode_on_air": "POST /api/programmes/{job_id}/air",
        "greeter": "GET /api/greeter",
        "station_stats": "GET /api/stats",
        "design_brief_gta": "GET /design/gta (form for the owner)",
        "the_player": "GET /player (the station's own front door)",
        "named_presence": "POST /api/presence",
        "summon_late_night": "POST /api/player/summon",
        "now_playing_page": "GET /now (human-friendly, with album art)",
        "album_art": "GET /api/nowplaying/art",
    }

    return web.json_response({
        "station": station,
        "where": where,
        "endpoints": _collect_endpoints(app),
    })
