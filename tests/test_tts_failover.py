"""Tests for per-voice TTS routing and failover.

Regression cover for the June 2026 outage: the one voice on air lived only on
the local Qwen host, that host died, and the station broadcast 41 days of music
with no DJ because a failed route had nowhere to go.
"""

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bridge.services.tts_service import TTSService

WAV = b"RIFF____WAVEfake-audio-payload"


def _tts_app(status: int = 200, body: bytes = WAV, record: list | None = None):
    """Minimal stand-in for a TTS backend."""
    async def handler(request: web.Request) -> web.Response:
        if record is not None:
            record.append(await request.json())
        if status != 200:
            return web.Response(status=status, text="backend on fire")
        return web.Response(body=body, content_type="audio/wav")

    app = web.Application()
    app.router.add_post("/tts", handler)
    return app


async def _serve(app) -> tuple[TestServer, str]:
    server = TestServer(app)
    await server.start_server()
    return server, str(server.make_url("/tts"))


@pytest.fixture
async def svc_factory(tmp_path, monkeypatch):
    """Build TTSServices with ffmpeg normalization stubbed out; closes them after."""
    created: list[TTSService] = []

    async def _noop(path):
        return None

    def build(**kwargs):
        kwargs.setdefault("endpoint", "http://127.0.0.1:1/tts")
        kwargs.setdefault("cache_dir", tmp_path)
        svc = TTSService(**kwargs)
        monkeypatch.setattr(svc, "_normalize_audio", _noop)
        created.append(svc)
        return svc

    yield build

    for svc in created:
        await svc.stop()


# =====================================================================
# ROUTING
# =====================================================================

def test_routes_primary_only_without_fallback(svc_factory):
    svc = svc_factory(endpoint="http://primary/tts")
    assert svc.routes_for("Eric") == [("http://primary/tts", "Eric")]


def test_routes_prefers_voice_map_then_fallback(svc_factory):
    svc = svc_factory(
        endpoint="http://primary/tts",
        voice_map={"laniv3": "http://box/api"},
        fallbacks={"laniv3": [{"endpoint": "http://primary/tts", "speaker": "Aiden"}]},
    )
    assert svc.routes_for("laniv3") == [
        ("http://box/api", "laniv3"),
        ("http://primary/tts", "Aiden"),
    ]


def test_fallback_may_substitute_speaker_on_another_host(svc_factory):
    """Voice names aren't portable between backends — the substitute matters."""
    svc = svc_factory(
        endpoint="http://qwen/tts",
        fallbacks={"Eric": [{"endpoint": "http://chatterbox/api", "speaker": "carlin"}]},
    )
    assert svc.routes_for("Eric") == [
        ("http://qwen/tts", "Eric"),
        ("http://chatterbox/api", "carlin"),
    ]


def test_duplicate_routes_are_collapsed(svc_factory):
    """A chain must never retry the same dead host twice."""
    svc = svc_factory(
        endpoint="http://primary/tts",
        fallbacks={"Eric": [{"speaker": "Eric"}, {"endpoint": "http://primary/tts"}]},
    )
    assert svc.routes_for("Eric") == [("http://primary/tts", "Eric")]


def test_malformed_fallback_entry_is_ignored(svc_factory):
    svc = svc_factory(endpoint="http://primary/tts", fallbacks={"Eric": ["nonsense"]})
    assert svc.routes_for("Eric") == [("http://primary/tts", "Eric")]


def test_known_endpoints_covers_map_and_fallbacks(svc_factory):
    svc = svc_factory(
        endpoint="http://primary/tts",
        voice_map={"snoop": "http://box/api"},
        fallbacks={"Eric": [{"endpoint": "http://third/api", "speaker": "x"}]},
    )
    assert svc.known_endpoints() == [
        "http://primary/tts", "http://box/api", "http://third/api",
    ]


# =====================================================================
# FAILOVER
# =====================================================================

async def test_primary_success_does_not_use_fallback(svc_factory):
    seen: list = []
    server, url = await _serve(_tts_app(record=seen))
    try:
        svc = svc_factory(
            endpoint=url,
            fallbacks={"Eric": [{"endpoint": "http://127.0.0.1:1/tts", "speaker": "x"}]},
        )
        await svc.start()
        path = await svc.speak("hello", speaker="Eric")

        assert path.read_bytes() == WAV
        assert [p["speaker"] for p in seen] == ["Eric"]
        assert svc.stats()["fallback_uses"] == 0
        assert svc.stats()["ever_succeeded"] is True
        await svc.stop()
    finally:
        await server.close()


async def test_dead_primary_fails_over_and_still_produces_audio(svc_factory):
    seen: list = []
    server, url = await _serve(_tts_app(record=seen))
    try:
        # Port 1 is closed — stands in for the dead Qwen host.
        svc = svc_factory(
            endpoint="http://127.0.0.1:1/tts",
            fallbacks={"Eric": [{"endpoint": url, "speaker": "carlin"}]},
        )
        await svc.start()
        path = await svc.speak("bob is back", speaker="Eric")

        assert path.read_bytes() == WAV
        # Served under the substitute voice, on the surviving host.
        assert [p["speaker"] for p in seen] == ["carlin"]
        stats = svc.stats()
        assert stats["fallback_uses"] == 1
        assert stats["consecutive_failures"] == 0
        assert stats["last_success_route"] == {"endpoint": url, "speaker": "carlin"}
        await svc.stop()
    finally:
        await server.close()


async def test_http_error_also_triggers_failover(svc_factory):
    """A sick backend must fall through, not just a refused connection."""
    sick, sick_url = await _serve(_tts_app(status=500))
    good_seen: list = []
    good, good_url = await _serve(_tts_app(record=good_seen))
    try:
        svc = svc_factory(
            endpoint=sick_url,
            fallbacks={"Eric": [{"endpoint": good_url, "speaker": "carlin"}]},
        )
        await svc.start()
        path = await svc.speak("still talking", speaker="Eric")

        assert path.read_bytes() == WAV
        assert [p["speaker"] for p in good_seen] == ["carlin"]
        assert svc.stats()["fallback_uses"] == 1
        await svc.stop()
    finally:
        await sick.close()
        await good.close()


async def test_empty_body_triggers_failover(svc_factory):
    empty, empty_url = await _serve(_tts_app(body=b""))
    good, good_url = await _serve(_tts_app())
    try:
        svc = svc_factory(
            endpoint=empty_url,
            fallbacks={"Eric": [{"endpoint": good_url, "speaker": "carlin"}]},
        )
        await svc.start()
        path = await svc.speak("hi", speaker="Eric")
        assert path.read_bytes() == WAV
        await svc.stop()
    finally:
        await empty.close()
        await good.close()


async def test_all_routes_dead_raises_and_counts_failure(svc_factory):
    svc = svc_factory(
        endpoint="http://127.0.0.1:1/tts",
        fallbacks={"Aiden": [{"endpoint": "http://127.0.0.1:2/tts", "speaker": "x"}]},
    )
    await svc.start()
    with pytest.raises(RuntimeError, match="all 2 route"):
        await svc.speak("nobody home")

    stats = svc.stats()
    assert stats["consecutive_failures"] == 1
    assert stats["ever_succeeded"] is False
    # The error names both attempts, so the log says what to go fix.
    assert "127.0.0.1:1" in stats["last_error"]
    assert "127.0.0.1:2" in stats["last_error"]
    await svc.stop()


async def test_failure_then_recovery_resets_counter(svc_factory):
    server, url = await _serve(_tts_app())
    try:
        svc = svc_factory(endpoint=url)
        await svc.start()
        svc._consecutive_failures = 7
        await svc.speak("recovered")
        assert svc.stats()["consecutive_failures"] == 0
        await svc.stop()
    finally:
        await server.close()


# =====================================================================
# HEALTH
# =====================================================================

async def test_probe_treats_any_http_response_as_alive(svc_factory):
    """Backends disagree about GET / — 404 still proves the process is up."""
    app = web.Application()  # no routes at all, so / returns 404
    server = TestServer(app)
    await server.start_server()
    try:
        svc = svc_factory()
        await svc.start()
        assert await svc.probe_endpoint(str(server.make_url("/tts/custom-voice"))) is True
        await svc.stop()
    finally:
        await server.close()


async def test_probe_reports_dead_host(svc_factory):
    svc = svc_factory()
    await svc.start()
    assert await svc.probe_endpoint("http://127.0.0.1:1/tts") is False
    await svc.stop()


async def test_health_report_covers_every_endpoint(svc_factory):
    server, url = await _serve(_tts_app())
    try:
        svc = svc_factory(endpoint=url, voice_map={"snoop": "http://127.0.0.1:1/tts"})
        await svc.start()
        report = await svc.health_report()
        assert report[url] is True
        assert report["http://127.0.0.1:1/tts"] is False
        await svc.stop()
    finally:
        await server.close()


async def test_silent_for_measures_from_start_before_any_success(svc_factory):
    """The restart-into-a-dead-host case must not read as healthy."""
    import time
    svc = svc_factory()
    svc._started_at = time.time() - 3600
    assert svc.silent_for_seconds == pytest.approx(3600, abs=5)
    assert svc.stats()["ever_succeeded"] is False


# =====================================================================
# CATCH-ALL FALLBACK
# =====================================================================

def test_unknown_voice_uses_the_default_fallback(svc_factory):
    """Enumerating fallbacks per known voice leaves an *unknown* one with
    nowhere to go. Snoop's `Adrian` was not on the local backend and not in the
    fallback table, so every one of his lines 422'd into silence — 33% of the
    station's talk, for two weeks, with no alert."""
    svc = svc_factory(
        endpoint="http://qwen/tts",
        fallbacks={"Eric": [{"endpoint": "http://box/api", "speaker": "carlin"}]},
        default_fallback={"endpoint": "http://box/api", "speaker": "carlin"},
    )
    assert svc.routes_for("Adrian") == [
        ("http://qwen/tts", "Adrian"),
        ("http://box/api", "carlin"),
    ]


def test_an_explicit_chain_beats_the_default(svc_factory):
    svc = svc_factory(
        endpoint="http://qwen/tts",
        fallbacks={"Adrian": [{"endpoint": "http://box/api", "speaker": "snoop"}]},
        default_fallback={"endpoint": "http://box/api", "speaker": "carlin"},
    )
    assert svc.routes_for("Adrian")[1] == ("http://box/api", "snoop")


def test_without_a_default_an_unknown_voice_has_one_route(svc_factory):
    svc = svc_factory(endpoint="http://qwen/tts")
    assert svc.routes_for("Adrian") == [("http://qwen/tts", "Adrian")]


async def test_unknown_voice_actually_survives_a_rejection(svc_factory):
    """The 422 case end to end: the primary refuses the voice, the catch-all
    carries the line."""
    seen: list = []
    sick, sick_url = await _serve(_tts_app(status=422))
    good, good_url = await _serve(_tts_app(record=seen))
    try:
        svc = svc_factory(endpoint=sick_url,
                          default_fallback={"endpoint": good_url, "speaker": "carlin"})
        await svc.start()
        path = await svc.speak("yeah", speaker="Adrian")
        assert path.read_bytes() == WAV
        assert [p["speaker"] for p in seen] == ["carlin"]
        await svc.stop()
    finally:
        await sick.close()
        await good.close()
