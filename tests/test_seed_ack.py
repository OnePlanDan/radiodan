"""Tests for the seed acknowledgement contract.

POST /api/producer/seed used to return 200 alongside the *previous* seed: the
handler waited for `producer._seed` to be non-null, which it always already was,
so it answered while the new seed was still queued behind the signal loop. In
practice a hip-hop seed went live 42 seconds after its own 200 response, and the
caller — an agent — had no way to tell whether it had taken.
"""

import asyncio

import pytest
from aiohttp import web

from bridge.web.routes.producer import _seed_wait_seconds, routes


class FakeSeed:
    def __init__(self, name):
        self.name = name

    def as_dict(self):
        return {"pipeline": self.name}


class FakeProducer:
    """Stands in for the producer plugin's seed-queue behaviour."""

    name = "producer"
    _running = True

    def __init__(self, live_seed="old"):
        self._seed = FakeSeed(live_seed)
        self._building = False
        self._characters = {"bob": object(), "lani": object()}
        self.submitted: list[dict] = []
        self._acks: list[asyncio.Future] = []

    async def submit_seed(self, raw):
        self.submitted.append(dict(raw))
        ack = asyncio.get_running_loop().create_future()
        self._acks.append(ack)
        return ack

    # --- helpers the tests use to drive the producer side ---
    def apply(self, name="new"):
        self._seed = FakeSeed(name)
        self._acks[-1].set_result({"applied": True, "error": ""})

    def reject(self, error="seed interpretation failed: boom"):
        self._acks[-1].set_result({"applied": False, "error": error})


@pytest.fixture
def client(aiohttp_client):
    async def _make(producer):
        app = web.Application()
        app["plugins"] = [producer]
        app.router.add_routes(routes)
        return await aiohttp_client(app)
    return _make


# =====================================================================
# WAIT PARAMETER
# =====================================================================

class _QueryOnlyRequest:
    def __init__(self, query):
        self.query = query


@pytest.mark.parametrize("query,expected", [
    ({}, 20.0),                 # default
    ({"wait": "0"}, 0.0),
    ({"wait": "5"}, 5.0),
    ({"wait": "-3"}, 0.0),      # clamped up
    ({"wait": "9999"}, 120.0),  # clamped down
    ({"wait": "banana"}, 20.0),  # junk falls back to the default
])
def test_wait_query_is_parsed_and_clamped(query, expected):
    assert _seed_wait_seconds(_QueryOnlyRequest(query)) == expected


# =====================================================================
# POST /api/producer/seed
# =====================================================================

async def test_applied_seed_reports_applied_and_the_new_seed(client):
    producer = FakeProducer(live_seed="old")
    c = await client(producer)

    async def drive():
        await asyncio.sleep(0.05)
        producer.apply("genre")

    task = asyncio.create_task(drive())
    resp = await c.post("/api/producer/seed", json={"genre": "hip-hop"})
    await task

    body = await resp.json()
    assert resp.status == 200
    assert body["ok"] is True
    assert body["applied"] is True
    assert body["pending"] is False
    assert body["error"] == ""
    # The regression: this must be the seed that is actually live.
    assert body["seed"] == {"pipeline": "genre"}


async def test_slow_seed_reports_pending_not_a_stale_seed(client):
    """The producer is busy; answer honestly instead of echoing the old seed."""
    producer = FakeProducer(live_seed="old")
    c = await client(producer)

    resp = await c.post("/api/producer/seed?wait=0.2", json={"genre": "hip-hop"})
    body = await resp.json()

    assert body["applied"] is False
    assert body["pending"] is True
    assert body["ok"] is True, "a queued seed was still accepted"
    # It reports the seed genuinely in effect, and pending tells you it's not yours.
    assert body["seed"] == {"pipeline": "old"}
    assert body["waited_seconds"] >= 0.2


async def test_rejected_seed_reports_the_error(client):
    producer = FakeProducer(live_seed="old")
    c = await client(producer)

    async def drive():
        await asyncio.sleep(0.05)
        producer.reject()

    task = asyncio.create_task(drive())
    resp = await c.post("/api/producer/seed", json={"genre": "hip-hop"})
    await task

    body = await resp.json()
    assert body["ok"] is False
    assert body["applied"] is False
    assert body["pending"] is False
    assert "interpretation failed" in body["error"]
    assert body["seed"] == {"pipeline": "old"}, "the old seed is still in effect"


async def test_wait_zero_returns_immediately_as_pending(client):
    producer = FakeProducer()
    c = await client(producer)
    resp = await c.post("/api/producer/seed?wait=0", json={"genre": "hip-hop"})
    body = await resp.json()
    assert body["pending"] is True
    assert body["waited_seconds"] < 0.1
    assert producer.submitted == [{"genre": "hip-hop"}], "still queued for real"


async def test_timeout_does_not_cancel_the_producers_ack(client):
    """shield() matters: the producer must still be able to apply the seed."""
    producer = FakeProducer()
    c = await client(producer)
    await c.post("/api/producer/seed?wait=0.1", json={"genre": "hip-hop"})

    ack = producer._acks[-1]
    assert not ack.cancelled(), "a timed-out request must not cancel the queued seed"
    producer.apply("genre")  # would raise InvalidStateError on a cancelled future
    assert ack.result()["applied"] is True


async def test_empty_body_is_still_rejected(client):
    producer = FakeProducer()
    c = await client(producer)
    resp = await c.post("/api/producer/seed", json={})
    assert resp.status == 400
    assert producer.submitted == []


# =====================================================================
# POST /api/producer/switch
# =====================================================================

async def test_switch_waits_for_the_seed_too(client):
    producer = FakeProducer()
    c = await client(producer)

    async def drive():
        await asyncio.sleep(0.05)
        producer.apply("character")

    task = asyncio.create_task(drive())
    resp = await c.post("/api/producer/switch", json={"character": "bob"})
    await task

    body = await resp.json()
    assert body["applied"] is True
    assert body["pending"] is False
    assert body["character"] == "bob"


async def test_switch_rejects_unknown_character_without_queueing(client):
    producer = FakeProducer()
    c = await client(producer)
    resp = await c.post("/api/producer/switch", json={"character": "nobody"})
    assert resp.status == 404
    assert producer.submitted == []
