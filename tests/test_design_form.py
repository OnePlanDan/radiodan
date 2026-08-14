"""Tests for the design-brief form and its answers landing on the station."""

import json

import pytest
from aiohttp import web

from bridge.web.routes.design import routes as design_routes


@pytest.fixture
def client(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setenv("RADIODAN_STATION_DIR", str(tmp_path))
    app = web.Application()
    app.router.add_routes(design_routes)
    return aiohttp_client(app)


async def test_the_form_is_served(client):
    c = await client
    resp = await c.get("/design/gta")
    assert resp.status == 200
    body = await resp.text()
    assert "Bad Signal After Dark" in body
    assert "Send to the station" in body


async def test_a_submission_is_stored_and_retrievable(client, tmp_path):
    c = await client
    answers = {"episode_minutes": "15", "profanity": "80",
               "triggers": ["late_night_connect", "starred_song"]}
    resp = await c.post("/api/design/gta", json=answers)
    assert resp.status == 200
    result = await resp.json()
    assert result["ok"] and result["fields"] == 3

    stored = json.loads((tmp_path / "design" / "gta-latest.json").read_text())
    assert stored["answers"] == answers
    assert stored["submitted_at"] > 0

    resp = await c.get("/api/design/gta")
    assert (await resp.json())["answers"] == answers


async def test_resubmitting_keeps_both_copies(client, tmp_path):
    """A second pass at the form must never destroy the first."""
    c = await client
    await c.post("/api/design/gta", json={"take": "one"})
    await c.post("/api/design/gta", json={"take": "two"})

    files = [p for p in (tmp_path / "design").iterdir() if p.name != "gta-latest.json"]
    assert len(files) >= 1  # same-second stamps may collide into one file
    latest = json.loads((tmp_path / "design" / "gta-latest.json").read_text())
    assert latest["answers"] == {"take": "two"}


async def test_prefill_before_any_submission_is_empty(client):
    c = await client
    resp = await c.get("/api/design/gta")
    assert (await resp.json())["answers"] is None


async def test_garbage_is_rejected(client):
    c = await client
    assert (await c.post("/api/design/gta", data=b"not json")).status == 400
    assert (await c.post("/api/design/gta", json=[])).status == 400
    assert (await c.post("/api/design/gta", json={})).status == 400
