"""Tests for the player's server side: identity, and summoning the show."""

import time

import pytest
from aiohttp import web

from bridge.web.routes.player import routes as player_routes


class FakeGreeter:
    def __init__(self):
        self.presence: list[tuple[str, str]] = []

    async def note_presence(self, name, device=""):
        self.presence.append((name, device))

    async def known_listeners(self, limit=20):
        return [{"name": n, "device": d, "first_seen": 1.0, "last_seen": 2.0}
                for n, d in self.presence]


class FakeCommissions:
    def __init__(self):
        self.rows: list[dict] = []
        self.aired: list[str] = []
        self.ordered: list[str] = []
        self.owned_shows = {"radiodan-latenight"}

    async def ready(self):
        return [r for r in self.rows if r["state"] == "ready"]

    async def pending(self):
        return [r for r in self.rows if r["state"] == "pending"]

    async def recent(self, limit=20):
        return self.rows

    def to_queue_item(self, row):
        return {"file_path": row["file_path"], "artist": row["show"],
                "title": row["title"], "genre": "programme",
                "duration_seconds": 600, "loudness_lufs": -16.0,
                "true_peak_dbfs": -1.5, "programme": True, "job_id": row["job_id"]}

    async def mark_aired(self, job_id):
        self.aired.append(job_id)

    async def commission(self, show, concept, location=None, **kw):
        row = {"job_id": f"j{len(self.rows)}", "show": show, "concept": concept,
               "state": "pending", "requested_at": time.time(),
               "file_path": None, "title": None}
        self.rows.append(row)
        self.ordered.append(concept)
        return row


class FakePlanner:
    def __init__(self):
        self.inserted = []

    async def insert_item(self, item, position=None):
        self.inserted.append((item, position))
        return True


class FakeMixer:
    def __init__(self):
        self.skips = 0

    async def next_track(self):
        self.skips += 1


class FakeStreamContext:
    def __init__(self):
        self.skip_sources = []
        self.current_track = {}

    async def notify_skip(self, source="listener"):
        self.skip_sources.append(source)


@pytest.fixture
def deps():
    return {
        "greeter": FakeGreeter(),
        "commissions": FakeCommissions(),
        "planner": FakePlanner(),
        "mixer": FakeMixer(),
        "stream_context": FakeStreamContext(),
    }


@pytest.fixture
def client(aiohttp_client, deps):
    app = web.Application()
    app["greeter"] = deps["greeter"]
    app["commissions"] = deps["commissions"]
    app["mixer"] = deps["mixer"]
    app["stream_context"] = deps["stream_context"]
    deps["stream_context"]._planner = deps["planner"]
    app.router.add_routes(player_routes)
    return aiohttp_client(app)


async def test_the_player_page_is_served(client):
    c = await client
    resp = await c.get("/player")
    assert resp.status == 200
    body = await resp.text()
    assert "Radio Dan" in body and "Summon Bad Signal" in body


async def test_presence_heartbeat_names_the_listener(client, deps):
    c = await client
    resp = await c.post("/api/presence", json={"name": "Dan", "device": "phone"})
    assert resp.status == 200
    assert deps["greeter"].presence == [("Dan", "phone")]

    resp = await c.get("/api/presence")
    listeners = (await resp.json())["listeners"]
    assert listeners[0]["name"] == "Dan"


async def test_presence_requires_a_name(client):
    c = await client
    assert (await c.post("/api/presence", json={"device": "phone"})).status == 400


async def test_summon_airs_a_ready_episode_with_a_system_skip(client, deps):
    deps["commissions"].rows.append({
        "job_id": "bs1", "show": "radiodan-latenight", "state": "ready",
        "file_path": "/music/_programmes/bs1.mp3", "title": "The Meter Wars",
        "requested_at": time.time(),
    })
    c = await client
    result = await (await c.post("/api/player/summon")).json()

    assert result["status"] == "on_air"
    assert deps["planner"].inserted[0][1] == 0
    assert deps["mixer"].skips == 1
    assert deps["stream_context"].skip_sources == ["system"], \
        "summoning must not read as the listener rejecting a song"
    assert deps["commissions"].aired == ["bs1"]


async def test_summon_reports_an_episode_already_building(client, deps):
    deps["commissions"].rows.append({
        "job_id": "bs2", "show": "radiodan-latenight", "state": "pending",
        "file_path": None, "title": None, "requested_at": time.time(),
    })
    c = await client
    result = await (await c.post("/api/player/summon")).json()
    assert result["status"] == "building"
    assert deps["commissions"].ordered == [], "no double order"


async def test_summon_orders_one_when_none_exists(client, deps):
    c = await client
    result = await (await c.post("/api/player/summon")).json()
    assert result["status"] == "ordered"
    assert "AMBUSH" in deps["commissions"].ordered[0]


async def test_summon_respects_the_daily_budget(client, deps):
    now = time.time()
    for i in range(3):
        deps["commissions"].rows.append({
            "job_id": f"old{i}", "show": "radiodan-morning", "state": "aired",
            "file_path": None, "title": None, "requested_at": now,
        })
    c = await client
    result = await (await c.post("/api/player/summon")).json()
    assert result["status"] == "budget_spent"
    assert deps["commissions"].ordered == []
