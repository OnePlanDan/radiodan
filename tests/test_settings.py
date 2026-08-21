"""Tests for the knobs: /api/settings and the live-apply/persist path.

House rule under test: settings changed in the GUI apply immediately and
survive a restart (persisted to the config store, replayed over YAML at boot).
"""

import pytest
from aiohttp import web

from bridge.web.routes.settings import routes as settings_routes


class FakeStore:
    def __init__(self):
        self.saved: dict = {}

    async def set(self, section, key, value):
        self.saved[(section, key)] = value

    async def get_section(self, section):
        return {k: v for (s, k), v in self.saved.items() if s == section}


class FakeMixer:
    async def get_volumes(self):
        return {"music_vol": 0.8, "duck_amount": 0.25, "crossfade_duration": 5.0}


class FakeCommissions:
    def __init__(self):
        self.max_per_day = 3
        self.auto_requeue = True
        self.poll_interval = 60.0
        self.owned_shows = {"radiodan-morning", "radiodan-latenight"}


@pytest.fixture
async def greeter(tmp_path):
    from bridge.services.greeter import GreeterService

    g = GreeterService(
        tracker=None, tts_service=None, mixer=None, voice_scheduler=None,
        planner=None, stream_context=None, db_path=tmp_path / "db",
        enabled=False,  # no poll task; settings logic is what's under test
        listener_name="Dan", cooldown_seconds=600.0,
    )
    await g.start()
    yield g
    await g.stop()


async def test_all_settings_in_one_read(aiohttp_client, greeter):
    app = web.Application()
    app["config_store"] = FakeStore()
    app["mixer"] = FakeMixer()
    app["commissions"] = FakeCommissions()
    app["greeter"] = greeter
    app.router.add_routes(settings_routes)
    c = await aiohttp_client(app)

    s = await (await c.get("/api/settings")).json()
    assert s["playback"]["duck_amount"] == 0.25
    assert s["greeter"]["cooldown_seconds"] == 600.0
    assert s["commissions"]["max_per_day"] == 3
    assert "radiodan-latenight" in s["commissions"]["owned_shows"]


async def test_greeter_knob_applies_live_and_persists(aiohttp_client, greeter):
    app = web.Application()
    store = FakeStore()
    app["config_store"] = store
    app["greeter"] = greeter
    app.router.add_routes(settings_routes)
    c = await aiohttp_client(app)

    resp = await c.put("/api/settings/greeter",
                       json={"cooldown_seconds": 120, "news_hour": 7})
    assert resp.status == 200
    assert greeter.cooldown_seconds == 120.0, "applied live"
    assert greeter.news_hour == 7
    assert store.saved[("greeter", "cooldown_seconds")] == 120.0, "persisted"


async def test_unknown_greeter_keys_are_ignored_not_applied(aiohttp_client, greeter):
    app = web.Application()
    app["config_store"] = FakeStore()
    app["greeter"] = greeter
    app.router.add_routes(settings_routes)
    c = await aiohttp_client(app)

    resp = await c.put("/api/settings/greeter",
                       json={"news_show": "lani-viv", "_db": "x", "cooldown_seconds": 60})
    assert resp.status == 200
    applied = (await resp.json())["applied"]
    assert "news_show" not in applied, "which show we own is not a slider"
    assert "_db" not in applied
    assert greeter.news_show != "lani-viv"


async def test_enabled_toggle_starts_and_stops_the_loop(aiohttp_client, greeter):
    app = web.Application()
    app["config_store"] = FakeStore()
    app["greeter"] = greeter
    app.router.add_routes(settings_routes)
    c = await aiohttp_client(app)

    assert greeter._task is None
    await c.put("/api/settings/greeter", json={"enabled": True})
    assert greeter._task is not None, "poll loop started without a restart"
    await c.put("/api/settings/greeter", json={"enabled": False})
    assert greeter._task is None, "and stopped again"


async def test_commission_budget_knob(aiohttp_client, greeter):
    app = web.Application()
    store = FakeStore()
    app["config_store"] = store
    app["commissions"] = FakeCommissions()
    app.router.add_routes(settings_routes)
    c = await aiohttp_client(app)

    resp = await c.put("/api/settings/commissions",
                       json={"max_per_day": 5, "auto_requeue": False})
    assert resp.status == 200
    assert app["commissions"].max_per_day == 5
    assert app["commissions"].auto_requeue is False
    assert store.saved[("commissions", "max_per_day")] == 5

    await c.put("/api/settings/commissions", json={"max_per_day": 0})
    assert app["commissions"].max_per_day == 1, "floor of one"


async def test_service_catalog_collects_every_endpoint_once(aiohttp_client):
    """"What are we calling?" must be one click — and deduplicated."""
    from bridge.web.routes.settings import _service_catalog

    class TTS:
        endpoint = "http://apollo:42001/tts/custom-voice"
        voice_map = {"laniv3": "http://chatterbox:11700/api/tts/custom"}
        fallbacks = {"Eric": [{"endpoint": "http://chatterbox:11700/api/tts/custom",
                               "speaker": "carlin"}]}
        default_fallback = {"endpoint": "http://chatterbox:11700/api/tts/custom",
                            "speaker": "carlin"}

    class STT:
        endpoint = "http://apollo:42002/transcribe"

    class Client:
        base_url = "http://mnemosyne:8100/api"

    class Comm:
        client = Client()

    class Tracker:
        status_url = "http://localhost:49996/status-json.xsl"

    app = web.Application()
    app["ctx_kwargs"] = {"tts_service": TTS(), "stt_service": STT()}
    app["commissions"] = Comm()
    app["listener_tracker"] = Tracker()

    class Req:
        pass
    req = Req()
    req.app = app
    services = _service_catalog(req)

    urls = [s["url"] for s in services]
    assert urls.count("http://chatterbox:11700/api/tts/custom") == 1, "deduplicated"
    assert "http://apollo:42001/tts/custom-voice" in urls
    assert "http://apollo:42002/transcribe" in urls
    assert "http://mnemosyne:8100/api" in urls
    assert "http://localhost:49996/status-json.xsl" in urls


async def test_the_settings_page_is_served(aiohttp_client):
    app = web.Application()
    app.router.add_routes(settings_routes)
    c = await aiohttp_client(app)
    resp = await c.get("/settings")
    assert resp.status == 200
    assert "knob" in (await resp.text())
