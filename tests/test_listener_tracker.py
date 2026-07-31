"""Tests for listener presence sampling.

Commissioning an episode costs ~20 minutes of build for ~7 minutes of audio, so
producing into an empty stream is the most expensive thing the station can do —
and some things (morning news) should air regardless. Both decisions need a
history of when someone was connected, and that history cannot be backfilled.
"""

import time

import pytest
from aiohttp import web

from bridge.audio.listener_tracker import ListenerTracker


def _icestats(source):
    return {"icestats": {"host": "radiodan.local", **({"source": source} if source is not None else {})}}


@pytest.fixture
async def icecast(aiohttp_server):
    """Fake Icecast whose reported state the test can change."""
    state = {"payload": _icestats({"listeners": 0, "listener_peak": 2}), "status": 200}

    async def handler(request):
        if state["status"] != 200:
            return web.Response(status=state["status"])
        return web.json_response(state["payload"])

    app = web.Application()
    app.router.add_get("/status-json.xsl", handler)
    server = await aiohttp_server(app)
    server.state = state
    return server


@pytest.fixture
async def tracker(icecast, tmp_path):
    t = ListenerTracker(
        db_path=tmp_path / "radiodan.db",
        status_url=str(icecast.make_url("/status-json.xsl")),
        interval=60.0,
    )
    await t.start()
    # The background loop takes its own samples; stop it so tests control timing.
    t._task.cancel()
    yield t
    await t.stop()


# =====================================================================
# READING ICECAST
# =====================================================================

async def test_reads_the_listener_count(tracker, icecast):
    icecast.state["payload"] = _icestats({"listeners": 3, "listener_peak": 5})
    assert await tracker.read_icecast() == (3, 5)


async def test_no_source_connected_is_unreadable_not_zero(tracker, icecast):
    """Icecast omits the key when nothing is streaming. That is not the same as
    'nobody is listening', and recording it as 0 would poison the pattern."""
    icecast.state["payload"] = _icestats(None)
    assert await tracker.read_icecast() is None


async def test_multiple_mounts_returns_a_list(tracker, icecast):
    icecast.state["payload"] = _icestats([{"listeners": 4, "listener_peak": 9}])
    assert await tracker.read_icecast() == (4, 9)


async def test_http_error_is_unreadable(tracker, icecast):
    icecast.state["status"] = 503
    assert await tracker.read_icecast() is None


async def test_unreachable_icecast_does_not_raise(tmp_path):
    t = ListenerTracker(db_path=tmp_path / "db", status_url="http://127.0.0.1:1/status-json.xsl")
    await t.start()
    t._task.cancel()
    assert await t.read_icecast() is None
    assert await t.sample_once() is None
    assert t.read_failures == 1
    await t.stop()


async def test_missing_peak_is_tolerated(tracker, icecast):
    icecast.state["payload"] = _icestats({"listeners": 1})
    assert await tracker.read_icecast() == (1, None)


# =====================================================================
# SAMPLING
# =====================================================================

async def test_sample_is_stored(tracker, icecast):
    icecast.state["payload"] = _icestats({"listeners": 2, "listener_peak": 2})
    assert await tracker.sample_once() == 2

    async with tracker._db.execute("SELECT listeners, peak FROM listener_samples") as cur:
        rows = [dict(r) async for r in cur]
    assert rows == [{"listeners": 2, "peak": 2}]


async def test_a_failed_read_stores_nothing(tracker, icecast):
    icecast.state["payload"] = _icestats(None)
    await tracker.sample_once()
    async with tracker._db.execute("SELECT COUNT(*) FROM listener_samples") as cur:
        assert (await cur.fetchone())[0] == 0


# =====================================================================
# THE QUESTIONS A SCHEDULER ASKS
# =====================================================================

async def _fill(tracker, entries):
    """entries: (seconds_ago, listeners)"""
    now = time.time()
    for ago, listeners in entries:
        await tracker._db.execute(
            "INSERT OR REPLACE INTO listener_samples (sampled_at, listeners, peak) VALUES (?,?,?)",
            (now - ago, listeners, listeners),
        )
    await tracker._db.commit()


async def test_reports_nobody_listening(tracker):
    await _fill(tracker, [(10, 0)])
    p = await tracker.presence()
    assert p["now"]["listening"] is False
    assert p["now"]["listeners"] == 0


async def test_reports_someone_listening(tracker):
    await _fill(tracker, [(10, 2)])
    p = await tracker.presence()
    assert p["now"]["listening"] is True
    assert p["now"]["listeners"] == 2


async def test_silence_duration_since_last_listener(tracker):
    await _fill(tracker, [(7200, 1), (10, 0)])
    p = await tracker.presence()
    assert p["silent_for_seconds"] == pytest.approx(7200, abs=30)


async def test_silence_is_none_when_never_heard(tracker):
    await _fill(tracker, [(60, 0), (120, 0)])
    p = await tracker.presence()
    assert p["last_heard_at"] is None
    assert p["silent_for_seconds"] is None


async def test_listening_minutes_derive_from_the_interval(tracker):
    """Each sample stands for one interval, so counting them gives time."""
    await _fill(tracker, [(60 * i, 1) for i in range(1, 11)])
    p = await tracker.presence()
    assert p["today"]["minutes_with_listeners"] == pytest.approx(10.0)


async def test_samples_with_nobody_do_not_count_as_listening(tracker):
    await _fill(tracker, [(60, 0), (120, 0), (180, 1)])
    p = await tracker.presence()
    assert p["today"]["minutes_with_listeners"] == pytest.approx(1.0)


async def test_today_and_week_are_separate_windows(tracker):
    await _fill(tracker, [(60, 1), (3 * 86400, 1)])
    p = await tracker.presence()
    assert p["today"]["minutes_with_listeners"] == pytest.approx(1.0)
    assert p["last_7_days"]["minutes_with_listeners"] == pytest.approx(2.0)


async def test_peak_is_reported(tracker):
    await _fill(tracker, [(60, 1), (120, 4), (180, 2)])
    p = await tracker.presence()
    assert p["today"]["peak_listeners"] == 4


async def test_typical_hours_covers_the_whole_clock(tracker):
    """A scheduler asks about a specific hour, so every hour must have an entry
    even when nobody has ever listened then."""
    await _fill(tracker, [(60, 1)])
    p = await tracker.presence()
    assert [h["hour"] for h in p["typical_hours"]] == list(range(24))


async def test_typical_hours_counts_distinct_days(tracker):
    """Two samples in the same hour on the same day is one day, not two —
    otherwise a single long session would look like a daily habit."""
    now = time.time()
    hour = int(time.strftime("%H", time.localtime(now)))
    await _fill(tracker, [(60, 1), (120, 1), (180, 1)])
    p = await tracker.presence()
    entry = next(h for h in p["typical_hours"] if h["hour"] == hour)
    assert entry["days_present"] == 1


async def test_sampling_metadata_is_exposed(tracker):
    await _fill(tracker, [(60, 1)])
    p = await tracker.presence()
    assert p["sampling"]["interval_seconds"] == 60.0
    assert p["sampling"]["samples_stored"] == 1
    assert p["sampling"]["observed_since"] is not None


async def test_presence_works_with_no_data_at_all(tracker):
    """First boot: must answer, not explode."""
    p = await tracker.presence()
    assert p["now"]["listening"] is False
    assert p["sampling"]["samples_stored"] == 0
    assert len(p["typical_hours"]) == 24


async def test_interval_has_a_floor(tmp_path):
    """Sampling every fraction of a second would hammer Icecast for no gain."""
    t = ListenerTracker(db_path=tmp_path / "db", status_url="http://x/", interval=0.1)
    assert t.interval >= 5.0
