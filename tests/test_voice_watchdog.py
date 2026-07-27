"""Tests for the voice watchdog.

The failure this guards against is not dead air — it is a station that looks
completely healthy while the DJ has stopped talking. In June-July 2026 that
state persisted for 41 days.
"""

import time

import pytest

from bridge.services.tts_service import TTSService
from bridge.services.voice_watchdog import VoiceWatchdog

THREE_HOURS = 3 * 3600


@pytest.fixture
async def tts(tmp_path):
    # Port 1 is closed, so _diagnose has a genuinely unreachable endpoint.
    svc = TTSService(endpoint="http://127.0.0.1:1/tts", cache_dir=tmp_path)
    yield svc
    await svc.stop()


@pytest.fixture
def watchdog(tts, event_store):
    return VoiceWatchdog(
        tts_service=tts, event_store=event_store,
        alert_after_seconds=THREE_HOURS, check_interval=1.0,
        reminder_interval=6 * 3600,
    )


async def _outage_rows(event_store):
    rows = await event_store.get_window(0.0, time.time() + 100)
    return [r for r in rows if r["event_type"] == "voice_outage"]


async def test_recent_success_does_not_alert(tts, watchdog):
    tts._last_success_at = time.time() - 600
    assert await watchdog.check_once() is False
    assert watchdog.status()["alerting"] is False


async def test_silence_past_threshold_alerts(tts, watchdog, event_store):
    tts._last_success_at = time.time() - 5 * 3600
    assert await watchdog.check_once() is True
    assert watchdog.status()["alerting"] is True

    rows = await _outage_rows(event_store)
    assert len(rows) == 1
    assert "silent" in rows[0]["title"]


async def test_repeat_checks_do_not_spam(tts, watchdog, event_store):
    """The 2026-05-08 checkup found a watchdog logging every 10s for six days."""
    tts._last_success_at = time.time() - 5 * 3600
    await watchdog.check_once()
    for _ in range(5):
        await watchdog.check_once()
    assert len(await _outage_rows(event_store)) == 1


async def test_reminder_fires_after_reminder_interval(tts, watchdog, event_store):
    tts._last_success_at = time.time() - 5 * 3600
    await watchdog.check_once()
    watchdog._last_alert_at = time.time() - (6 * 3600 + 1)
    await watchdog.check_once()
    # Reminds via the log, but does not open a second outage record.
    assert len(await _outage_rows(event_store)) == 1
    assert watchdog._last_alert_at == pytest.approx(time.time(), abs=5)


async def test_recovery_clears_alert_and_closes_event(tts, watchdog, event_store):
    tts._last_success_at = time.time() - 5 * 3600
    await watchdog.check_once()
    assert watchdog._alerting is True

    tts._last_success_at = time.time()
    assert await watchdog.check_once() is False
    assert watchdog._alerting is False

    rows = await _outage_rows(event_store)
    assert rows[0]["status"] == "completed"
    assert rows[0]["ended_at"] is not None


async def test_second_outage_opens_a_new_event(tts, watchdog, event_store):
    tts._last_success_at = time.time() - 5 * 3600
    await watchdog.check_once()
    tts._last_success_at = time.time()
    await watchdog.check_once()
    tts._last_success_at = time.time() - 5 * 3600
    await watchdog.check_once()
    assert len(await _outage_rows(event_store)) == 2


async def test_never_succeeded_alerts_from_process_start(tts, watchdog):
    """Restart into a dead TTS host — the exact June 2026 shape."""
    tts._started_at = time.time() - 41 * 86400
    assert tts._last_success_at is None
    assert await watchdog.check_once() is True

    status = watchdog.status()
    assert status["ever_succeeded"] is False
    assert status["silent_for"].startswith("41d")


async def test_diagnosis_names_the_dead_endpoint(tts, watchdog):
    detail = await watchdog._diagnose()
    assert "127.0.0.1:1" in detail
    assert "Unreachable" in detail


async def test_diagnosis_when_hosts_are_up_points_at_generation(tmp_path, event_store):
    """Endpoints reachable but voice still failing is a different problem."""
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    server = TestServer(web.Application())
    await server.start_server()
    try:
        svc = TTSService(endpoint=str(server.make_url("/tts")), cache_dir=tmp_path)
        await svc.start()
        svc._last_error = "HTTP 422: voice not found"
        wd = VoiceWatchdog(tts_service=svc, event_store=event_store)
        detail = await wd._diagnose()
        assert "not a dead host" in detail
        assert "voice not found" in detail
        await svc.stop()
    finally:
        await server.close()


async def test_status_payload_shape(tts, watchdog):
    status = watchdog.status()
    for key in (
        "alerting", "silent_for_seconds", "silent_for", "alert_after_seconds",
        "ever_succeeded", "consecutive_failures", "fallback_uses", "last_error",
    ):
        assert key in status


async def test_start_and_stop_are_idempotent(watchdog):
    await watchdog.start()
    first = watchdog._task
    await watchdog.start()
    assert watchdog._task is first
    await watchdog.stop()
    assert watchdog._task is None
    await watchdog.stop()


async def test_broken_check_does_not_kill_the_loop(tts, watchdog, monkeypatch):
    """A watchdog that crashes must not take the station down with it."""
    import asyncio

    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(watchdog, "check_once", boom)
    watchdog.check_interval = 0.01
    await watchdog.start()
    await asyncio.sleep(0.15)
    assert calls["n"] > 1, "loop should survive an exception and keep checking"
    assert not watchdog._task.done()
    await watchdog.stop()
