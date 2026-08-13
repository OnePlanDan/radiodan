"""Tests for the AudioSegment client.

AudioSegment produces finished episodes from a brief. Its measured history — 591
episodes, `build ≈ 13.0 + 1.04 × length` minutes — is why the station treats a
commission as something planned an hour ahead rather than requested at air time.
"""

import pytest
from aiohttp import web

from bridge.services.audiosegment import (
    AudioSegmentClient,
    AudioSegmentError,
    estimated_build_minutes,
    is_pending,
    is_terminal,
)


@pytest.fixture
async def service(aiohttp_server):
    """Fake AudioSegment whose responses the test controls."""
    state = {
        "health": {"status": "healthy", "database": "ok"},
        "shows": [{"name": "lani-viv", "display_name": "Lani & Viv", "episode_count": 558}],
        "produce": {"status": "queued", "job_id": "job-123", "position": 0},
        "job": {"job_id": "job-123", "status": "queued", "show_name": "lani-viv"},
        "audio": b"ID3fake-mp3-bytes",
        "audio_status": 200,
        "produce_status": 200,
        "requests": [],
    }

    async def health(request):
        return web.json_response(state["health"])

    async def shows(request):
        return web.json_response(state["shows"])

    async def produce(request):
        state["requests"].append(("produce", request.match_info["show"], await request.json()))
        if state["produce_status"] != 200:
            return web.Response(status=state["produce_status"], text="nope")
        return web.json_response(state["produce"])

    async def job(request):
        return web.json_response(state["job"])

    async def audio(request):
        if state["audio_status"] != 200:
            return web.Response(status=state["audio_status"], text="not ready")
        return web.Response(body=state["audio"], content_type="audio/mpeg")

    async def feedback(request):
        state["requests"].append(("feedback", request.match_info["job_id"], await request.json()))
        return web.json_response({"ok": True})

    async def requeue(request):
        state["requests"].append(("requeue", request.match_info["job_id"], None))
        return web.json_response({"status": "queued"})

    app = web.Application()
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/shows", shows)
    app.router.add_post("/api/shows/{show}/produce", produce)
    app.router.add_get("/api/jobs/{job_id}", job)
    app.router.add_get("/api/jobs/{job_id}/audio", audio)
    app.router.add_post("/api/jobs/{job_id}/feedback", feedback)
    app.router.add_post("/api/jobs/{job_id}/requeue", requeue)

    server = await aiohttp_server(app)
    server.state = state
    return server


@pytest.fixture
async def client(service):
    c = AudioSegmentClient(base_url=str(service.make_url("/api")), timeout=5.0)
    await c.start()
    yield c
    await c.stop()


# =====================================================================
# DISCOVERY
# =====================================================================

async def test_health(client):
    assert (await client.health())["status"] == "healthy"
    assert await client.is_healthy() is True


async def test_unhealthy_service_reports_false_not_an_exception(client, service):
    service.state["health"] = {"status": "degraded"}
    assert await client.is_healthy() is False


async def test_unreachable_service_reports_false(client):
    client.base_url = "http://127.0.0.1:1/api"
    assert await client.is_healthy() is False


async def test_shows(client):
    shows = await client.shows()
    assert shows[0]["name"] == "lani-viv"


async def test_shows_accepts_a_wrapped_list(client, service):
    service.state["shows"] = {"shows": [{"name": "bobs-boat"}]}
    assert (await client.shows())[0]["name"] == "bobs-boat"


# =====================================================================
# COMMISSIONING
# =====================================================================

async def test_produce_returns_a_job(client):
    result = await client.produce("lani-viv", "A ship not on the manifest")
    assert result["job_id"] == "job-123"


async def test_produce_sends_only_the_brief(client, service):
    """The production side owns how an episode is written; the station sends a
    concept, not a script."""
    await client.produce("lani-viv", "A ship not on the manifest")
    kind, show, body = service.state["requests"][-1]
    assert (kind, show) == ("produce", "lani-viv")
    assert body == {"concept": "A ship not on the manifest"}


async def test_produce_passes_optional_context(client, service):
    await client.produce("lani-viv", "c", location="Gothenburg", weight=7, context_mode="sharp")
    _, _, body = service.state["requests"][-1]
    assert body["location"] == "Gothenburg"
    assert body["weight"] == 7
    assert body["context_mode"] == "sharp"


async def test_produce_omits_unset_options(client, service):
    await client.produce("lani-viv", "c")
    _, _, body = service.state["requests"][-1]
    assert "location" not in body and "weight" not in body


async def test_produce_without_a_job_id_is_an_error(client, service):
    service.state["produce"] = {"status": "queued"}
    with pytest.raises(AudioSegmentError, match="no job_id"):
        await client.produce("lani-viv", "c")


async def test_http_error_becomes_a_clean_exception(client, service):
    service.state["produce_status"] = 500
    with pytest.raises(AudioSegmentError, match="500"):
        await client.produce("lani-viv", "c")


async def test_job_status(client, service):
    service.state["job"] = {"job_id": "job-123", "status": "synthesizing"}
    assert (await client.job("job-123"))["status"] == "synthesizing"


async def test_feedback_and_requeue(client, service):
    await client.feedback("job-123", 1, "aired well")
    assert service.state["requests"][-1] == ("feedback", "job-123", {"rating": 1, "comment": "aired well"})
    await client.requeue("job-123")
    assert service.state["requests"][-1][0] == "requeue"


# =====================================================================
# DELIVERY
# =====================================================================

async def test_download_writes_the_audio(client, tmp_path):
    dest = tmp_path / "ep.mp3"
    await client.download_audio("job-123", dest)
    assert dest.read_bytes() == b"ID3fake-mp3-bytes"


async def test_download_leaves_no_partial_file_behind(client, tmp_path):
    """A half-written file must never look like a deliverable to whatever scans
    the directory."""
    dest = tmp_path / "ep.mp3"
    await client.download_audio("job-123", dest)
    assert list(tmp_path.glob("*.part")) == []


async def test_download_before_completion_is_an_error(client, service, tmp_path):
    service.state["audio_status"] = 409
    with pytest.raises(AudioSegmentError, match="not finished"):
        await client.download_audio("job-123", tmp_path / "ep.mp3")
    assert not (tmp_path / "ep.mp3").exists()


async def test_empty_download_is_rejected(client, service, tmp_path):
    service.state["audio"] = b""
    with pytest.raises(AudioSegmentError, match="empty"):
        await client.download_audio("job-123", tmp_path / "ep.mp3")
    assert list(tmp_path.glob("*")) == []


async def test_download_creates_the_directory(client, tmp_path):
    dest = tmp_path / "nested" / "deeper" / "ep.mp3"
    await client.download_audio("job-123", dest)
    assert dest.exists()


# =====================================================================
# STATUS HELPERS
# =====================================================================

@pytest.mark.parametrize("status", [
    "queued", "researching", "scripting", "scripted", "synthesizing", "mastering",
])
def test_pipeline_statuses_are_pending(status):
    assert is_pending(status) is True
    assert is_terminal(status) is False


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_end_statuses_are_terminal(status):
    assert is_terminal(status) is True
    assert is_pending(status) is False


def test_unknown_status_is_neither():
    """An unrecognised status must not be mistaken for done — that would air
    nothing and mark the commission finished."""
    assert is_pending("something_new") is False
    assert is_terminal("something_new") is False


# =====================================================================
# BUILD TIME
# =====================================================================

def test_build_estimate_matches_the_measured_fit():
    assert estimated_build_minutes(7) == pytest.approx(20.3, abs=0.1)
    assert estimated_build_minutes(25) == pytest.approx(39.0, abs=0.1)


def test_short_episodes_are_the_expensive_shape():
    """13 min of fixed overhead: 2 minutes of audio costs ~7.5x realtime, which
    is why the station commissions long blocks."""
    assert estimated_build_minutes(2) / 2 > 7
    assert estimated_build_minutes(25) / 25 < 2


def test_estimate_is_never_negative():
    assert estimated_build_minutes(-5) == pytest.approx(13.0)
