"""Tests for agent-facing discovery and history.

Before these, `/`, `/api` and `/api/` all 404'd, so anything arriving without a
hand-written endpoint list had no way to find out what the station could do — seen
in the access log as an `agentos-probe` hitting `/` and getting a 404. And event
history was SSE-only, so a one-off "what just aired" meant holding a stream open
and accepting a fixed window.
"""

import time

import pytest
from aiohttp import web

from bridge.web.routes.events import routes as event_routes
from bridge.web.routes.index import routes as index_routes


@pytest.fixture
def app(event_store):
    app = web.Application()
    app["event_store"] = event_store
    app["station_name"] = "Radio Dan"
    app["stream_url"] = "http://localhost:49996/stream"
    app["start_time"] = time.time() - 3600

    # A couple of stand-in routes so the index has something to discover beyond
    # itself, including one with a docstring to check summaries are picked up.
    extra = web.RouteTableDef()

    @extra.get("/api/status")
    async def status(request):
        """Current playback state, track info, and active plugins."""
        return web.json_response({})

    @extra.post("/api/producer/seed")
    async def seed(request):
        """Set a new seed."""
        return web.json_response({})

    @extra.get("/api/undocumented")
    async def undocumented(request):
        return web.json_response({})

    app.router.add_routes(extra)
    app.router.add_routes(event_routes)
    app.router.add_routes(index_routes)
    return app


@pytest.fixture
def client(aiohttp_client, app):
    return aiohttp_client(app)


# =====================================================================
# DISCOVERY
# =====================================================================

@pytest.mark.parametrize("path", ["/", "/api", "/api/"])
async def test_index_answers_on_every_entry_point(client, path):
    """`/` included deliberately — it is the first thing anything probes."""
    c = await client
    resp = await c.get(path)
    assert resp.status == 200, f"{path} must not 404"
    body = await resp.json()
    assert "endpoints" in body


async def test_index_is_generated_from_the_live_route_table(client):
    """The hand-written version drifted in weeks; this one cannot."""
    c = await client
    body = await (await c.get("/api/")).json()
    paths = {(e["method"], e["path"]) for e in body["endpoints"]}

    assert ("GET", "/api/status") in paths
    assert ("POST", "/api/producer/seed") in paths
    assert ("GET", "/api/events/history") in paths
    # A route registered but never mentioned in any docs still shows up.
    assert ("GET", "/api/undocumented") in paths


async def test_index_lists_itself(client):
    c = await client
    body = await (await c.get("/api/")).json()
    assert ("GET", "/api/") in {(e["method"], e["path"]) for e in body["endpoints"]}


async def test_index_omits_head_and_options(client):
    """aiohttp adds HEAD to every GET; OPTIONS is middleware. Neither is useful."""
    c = await client
    body = await (await c.get("/api/")).json()
    methods = {e["method"] for e in body["endpoints"]}
    assert "HEAD" not in methods
    assert "OPTIONS" not in methods


async def test_summaries_come_from_docstrings(client):
    c = await client
    body = await (await c.get("/api/")).json()
    entry = next(e for e in body["endpoints"] if e["path"] == "/api/status")
    assert entry["summary"] == "Current playback state, track info, and active plugins."


async def test_undocumented_route_gets_an_empty_summary_not_an_error(client):
    c = await client
    body = await (await c.get("/api/")).json()
    entry = next(e for e in body["endpoints"] if e["path"] == "/api/undocumented")
    assert entry["summary"] == ""


async def test_seed_body_fields_are_inlined(client):
    """One call to get oriented, rather than needing a second schema endpoint."""
    c = await client
    body = await (await c.get("/api/")).json()
    entry = next(e for e in body["endpoints"]
                 if e["path"] == "/api/producer/seed" and e["method"] == "POST")
    assert "genre" in entry["fields"]
    assert "hard" in entry["fields"]
    assert "wait" in entry["query"]


async def test_index_orients_the_caller(client):
    c = await client
    body = await (await c.get("/api/")).json()
    assert body["station"]["name"] == "Radio Dan"
    assert body["station"]["stream_url"].endswith("/stream")
    assert body["station"]["uptime_seconds"] > 0
    # Pointers, so an agent learns where state lives rather than being handed a copy.
    assert body["where"]["now_playing"] == "GET /api/status"
    assert body["where"]["change_the_show"] == "POST /api/producer/seed"


async def test_endpoints_are_sorted_stably(client):
    c = await client
    body = await (await c.get("/api/")).json()
    keys = [(e["path"], e["method"]) for e in body["endpoints"]]
    assert keys == sorted(keys)


# =====================================================================
# HISTORY
# =====================================================================

async def _seed_events(event_store, now):
    await event_store.start_event("track_play", "music", "Recent song", started_at=now - 60)
    await event_store.start_event("voice_segment", "producer", "Recent talk", started_at=now - 120)
    await event_store.start_event("track_play", "music", "Old song", started_at=now - 7200)
    await event_store.start_event("voice_segment", "time", "Booong", started_at=now - 300)


async def test_history_returns_json_not_a_stream(client, event_store):
    now = time.time()
    await _seed_events(event_store, now)
    c = await client
    resp = await c.get("/api/events/history")
    assert resp.status == 200
    assert resp.content_type == "application/json"
    body = await resp.json()
    assert body["count"] >= 1


async def test_history_window_excludes_older_events(client, event_store):
    now = time.time()
    await _seed_events(event_store, now)
    c = await client
    body = await (await c.get("/api/events/history?minutes=60")).json()
    titles = [e["title"] for e in body["events"]]
    assert "Recent song" in titles
    assert "Old song" not in titles, "2 hours ago is outside a 60 minute window"


async def test_stale_never_closed_events_do_not_leak_in(client, event_store):
    """The station has hundreds of voice segments stranded without an ended_at.
    The event store treats those as still running, so they match any window —
    correct for a live timeline, wrong for "what aired in the last hour"."""
    now = time.time()
    await event_store.start_event(
        "voice_segment", "producer", "Stranded days ago", started_at=now - 5 * 86400)
    c = await client
    body = await (await c.get("/api/events/history?minutes=60")).json()
    assert "Stranded days ago" not in [e["title"] for e in body["events"]]


async def test_history_window_is_caller_chosen(client, event_store):
    now = time.time()
    await _seed_events(event_store, now)
    c = await client
    body = await (await c.get("/api/events/history?minutes=240")).json()
    assert "Old song" in [e["title"] for e in body["events"]]


async def test_history_is_newest_first(client, event_store):
    now = time.time()
    await _seed_events(event_store, now)
    c = await client
    body = await (await c.get("/api/events/history?minutes=600")).json()
    starts = [e["started_at"] for e in body["events"]]
    assert starts == sorted(starts, reverse=True), '"what just happened" is the usual question'


async def test_history_lane_filter(client, event_store):
    now = time.time()
    await _seed_events(event_store, now)
    c = await client
    body = await (await c.get("/api/events/history?minutes=600&lane=producer")).json()
    assert {e["lane"] for e in body["events"]} == {"producer"}
    assert body["window"]["lanes"] == ["producer"]


async def test_history_accepts_repeated_lane_params(client, event_store):
    now = time.time()
    await _seed_events(event_store, now)
    c = await client
    body = await (await c.get("/api/events/history?minutes=600&lane=producer&lane=time")).json()
    assert {e["lane"] for e in body["events"]} == {"producer", "time"}


async def test_history_limit_and_truncation_flag(client, event_store):
    now = time.time()
    for i in range(10):
        await event_store.start_event("track_play", "music", f"Song {i}", started_at=now - i)
    c = await client
    body = await (await c.get("/api/events/history?limit=3")).json()
    assert body["count"] == 3
    assert len(body["events"]) == 3
    assert body["truncated"] is True


async def test_history_reports_not_truncated_when_it_fits(client, event_store):
    now = time.time()
    await _seed_events(event_store, now)
    c = await client
    body = await (await c.get("/api/events/history?minutes=600&limit=1000")).json()
    assert body["truncated"] is False


@pytest.mark.parametrize("query,expected_minutes", [
    ("", 60.0),
    ("?minutes=5", 5.0),
    ("?minutes=99999", 1440.0),     # clamped
    ("?minutes=0", 1.0),            # clamped
    ("?minutes=banana", 60.0),      # junk falls back
])
async def test_history_query_bounds(client, query, expected_minutes):
    """The event log holds months of rows, so unbounded queries are a footgun."""
    c = await client
    body = await (await c.get(f"/api/events/history{query}")).json()
    assert body["window"]["minutes"] == expected_minutes


async def test_history_limit_is_bounded(client, event_store):
    c = await client
    body = await (await c.get("/api/events/history?limit=999999")).json()
    assert len(body["events"]) <= 1000
