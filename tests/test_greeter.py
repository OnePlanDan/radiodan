"""Tests for the greeter — the station noticing a listener arriving.

The contract: any arrival gets a greeting (with a cooldown so a flappy car
connection doesn't machine-gun the listener), the day's FIRST arrival breaks
the song for a freshly built bulletin, the bulletin is ordered daily whether
anyone listens, and every failure degrades to silence-about-it, never to a
broken stream.
"""

import asyncio
import time
from pathlib import Path

import pytest

from bridge.services.greeter import (
    ARRIVAL,
    BULLETIN_AIR,
    FIRST_OF_DAY,
    GreeterService,
    build_greeting,
)


# =====================================================================
# FAKES
# =====================================================================

class FakeTracker:
    def __init__(self):
        self.reading = (0, 0)

    async def read_icecast(self):
        return self.reading


class FakeTTS:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.spoken: list[str] = []
        self.fail = False

    async def speak(self, text, speaker=None, instruct=None):
        if self.fail:
            raise RuntimeError("tts down")
        self.spoken.append(text)
        p = self.tmp / f"tts-{len(self.spoken)}.wav"
        p.write_bytes(b"RIFFfake")
        return p


class FakeMixer:
    def __init__(self):
        self.commands: list[str] = []
        self.skips = 0

    async def _send_command(self, cmd):
        self.commands.append(cmd)
        return "OK"

    async def next_track(self):
        self.skips += 1
        return True


class FakeScheduler:
    def __init__(self):
        self.segments = []

    async def submit(self, segment):
        self.segments.append(segment)


class FakePlanner:
    def __init__(self):
        self.inserted: list[tuple[dict, int | None]] = []
        self.refuse = False

    async def insert_item(self, item, position=None):
        if self.refuse:
            return False
        self.inserted.append((item, position))
        return True


class FakeStreamContext:
    def __init__(self):
        self.skips_notified = 0

    async def notify_skip(self):
        self.skips_notified += 1


class FakeCommissions:
    def __init__(self, owned=("radiodan-morning",)):
        self.owned_shows = set(owned)
        self.rows: list[dict] = []
        self.ordered: list[str] = []
        self.aired: list[str] = []
        self.fail_orders = False

    async def latest_for_show(self, show, since_ts=None):
        matches = [
            r for r in self.rows
            if r["show"] == show and (since_ts is None or r["requested_at"] >= since_ts)
        ]
        return max(matches, key=lambda r: r["requested_at"]) if matches else None

    async def commission(self, show, concept, location=None, **kw):
        if self.fail_orders:
            raise RuntimeError("service down")
        row = {
            "job_id": f"job-{len(self.rows) + 1}",
            "show": show,
            "concept": concept,
            "state": "pending",
            "requested_at": time.time(),
            "file_path": None,
            "title": None,
            "duration_seconds": None,
            "loudness_lufs": None,
            "true_peak_dbfs": None,
        }
        self.rows.append(row)
        self.ordered.append(concept)
        return row

    def make_ready(self, title="The Day So Far"):
        row = {
            "job_id": f"job-{len(self.rows) + 1}",
            "show": "radiodan-morning",
            "concept": "c",
            "state": "ready",
            "requested_at": time.time(),
            "file_path": "/music/_programmes/x.mp3",
            "title": title,
            "duration_seconds": 300.0,
            "loudness_lufs": -16.0,
            "true_peak_dbfs": -1.5,
        }
        self.rows.append(row)
        return row

    def to_queue_item(self, row):
        return {
            "file_path": row["file_path"],
            "artist": row["show"],
            "title": row["title"] or row["concept"],
            "genre": "programme",
            "duration_seconds": row["duration_seconds"],
            "loudness_lufs": row["loudness_lufs"],
            "true_peak_dbfs": row["true_peak_dbfs"],
            "programme": True,
            "job_id": row["job_id"],
        }

    async def mark_aired(self, job_id):
        self.aired.append(job_id)
        for r in self.rows:
            if r["job_id"] == job_id:
                r["state"] = "aired"


class FakeStats:
    async def snapshot(self):
        return {"songs_played_today": 42, "disk_free_gb": 120.0, "library_tracks": 7702}

    async def songs_since(self, ts):
        return 17


@pytest.fixture
async def greeter(tmp_path, monkeypatch):
    # The fake TTS output is not real audio; duration probing is not under test.
    async def _instant(path):
        return 0.0
    monkeypatch.setattr("bridge.services.greeter._audio_duration", _instant)

    g = GreeterService(
        tracker=FakeTracker(),
        tts_service=FakeTTS(tmp_path),
        mixer=FakeMixer(),
        voice_scheduler=FakeScheduler(),
        planner=FakePlanner(),
        stream_context=FakeStreamContext(),
        db_path=tmp_path / "radiodan.db",
        commissions=FakeCommissions(),
        stats=FakeStats(),
        listener_name="Dan",
        poll_interval=10.0,
        cooldown_seconds=180.0,
        news_show="radiodan-morning",
        news_hour=0,  # "past the hour" all day, so daily ordering is active
        location="Gothenburg, Sweden",
    )
    await g.start()
    g._task.cancel()  # tests drive tick() explicitly
    yield g
    await g.stop()


async def _drain_air_task(g):
    if g._air_task:
        await g._air_task


# =====================================================================
# ARRIVALS
# =====================================================================

async def test_connecting_gets_a_greeting(greeter):
    greeter.tracker.reading = (0, 0)
    await greeter.tick()
    greeter.tracker.reading = (1, 1)
    await greeter.tick()

    assert len(greeter.voice_scheduler.segments) == 1
    assert "welcome" in greeter.voice_scheduler.segments[0].text.lower() or \
           "back" in greeter.voice_scheduler.segments[0].text.lower()


async def test_a_steady_listener_is_not_re_greeted(greeter):
    greeter.tracker.reading = (0, 0)
    await greeter.tick()
    greeter.tracker.reading = (1, 1)
    await greeter.tick()
    await _drain_air_task(greeter)
    await greeter.tick()
    await greeter.tick()

    greeted = [s for s in greeter.voice_scheduler.segments if "bulletin" not in s.text.lower() or True]
    assert greeter.greetings_sent == 1


async def test_disconnecting_says_nothing(greeter):
    greeter.tracker.reading = (1, 1)
    greeter._last_listeners = 1
    greeter.tracker.reading = (0, 0)
    await greeter.tick()
    assert greeter.greetings_sent == 0


async def test_reconnecting_within_cooldown_stays_quiet(greeter):
    """Stopping at a red light must not produce a greeting per restart."""
    greeter.tracker.reading = (0, 0)
    await greeter.tick()
    greeter.tracker.reading = (1, 1)
    await greeter.tick()
    await _drain_air_task(greeter)

    greeter.tracker.reading = (0, 0)
    await greeter.tick()
    greeter.tracker.reading = (1, 1)
    await greeter.tick()

    assert greeter.greetings_sent == 1


async def test_reconnecting_after_cooldown_greets_again(greeter):
    greeter.cooldown_seconds = 0.0
    greeter.tracker.reading = (0, 0)
    await greeter.tick()
    greeter.tracker.reading = (1, 1)
    await greeter.tick()
    await _drain_air_task(greeter)

    greeter.tracker.reading = (0, 0)
    await greeter.tick()
    greeter.tracker.reading = (1, 1)
    await greeter.tick()

    assert greeter.greetings_sent == 2


async def test_boot_with_listener_and_recent_greeting_stays_quiet(greeter):
    """A bridge restart mid-listen must not re-greet the same session."""
    await greeter._log(ARRIVAL, "before the restart")
    greeter.tracker.reading = (1, 1)  # first reading after boot: already connected
    await greeter.tick()
    assert greeter.greetings_sent == 0


async def test_boot_with_listener_and_no_recent_greeting_greets(greeter):
    greeter.tracker.reading = (1, 1)
    await greeter.tick()
    await _drain_air_task(greeter)
    assert greeter.greetings_sent == 1


async def test_tts_failure_degrades_to_no_greeting(greeter):
    greeter.tts_service.fail = True
    greeter.tracker.reading = (0, 0)
    await greeter.tick()
    greeter.tracker.reading = (1, 1)
    await greeter.tick()

    assert greeter.greetings_sent == 0
    assert greeter.voice_scheduler.segments == []


async def test_greeting_updates_the_icy_text(greeter):
    result = await greeter.greet(force=True)
    assert result["greeted"]
    assert any("music.set_metadata" in c for c in greeter.mixer.commands)


# =====================================================================
# FIRST OF THE DAY
# =====================================================================

async def test_first_arrival_of_the_day_is_marked_as_such(greeter):
    r1 = await greeter.greet(force=True)
    await _drain_air_task(greeter)
    r2 = await greeter.greet(force=True)

    assert r1["kind"] == FIRST_OF_DAY
    assert r2["kind"] == ARRIVAL


async def test_first_of_day_with_a_ready_bulletin_breaks_the_song(greeter):
    row = greeter.commissions.make_ready(title="The Day So Far")
    result = await greeter.greet(force=True)
    await _drain_air_task(greeter)

    assert result["bulletin"] == "ready"
    assert result["bulletin_airing"] is True
    item, position = greeter.planner.inserted[0]
    assert position == 0
    assert item["title"] == "The Day So Far"
    assert greeter.mixer.skips == 1, "the current song is broken"
    assert greeter.stream_context.skips_notified == 1
    assert greeter.commissions.aired == [row["job_id"]]


async def test_first_of_day_without_a_bulletin_orders_one(greeter):
    result = await greeter.greet(force=True)

    assert result["bulletin"] == "ordered"
    assert len(greeter.commissions.ordered) == 1
    assert "bulletin" in result["text"].lower()
    assert greeter.planner.inserted == [], "nothing airs before it exists"


async def test_second_arrival_of_the_day_does_not_reorder(greeter):
    await greeter.greet(force=True)          # orders the catch-up
    await _drain_air_task(greeter)
    greeter.cooldown_seconds = 0.0
    await greeter.greet(force=True)          # plain arrival

    assert len(greeter.commissions.ordered) == 1


async def test_ordering_failure_still_greets(greeter):
    greeter.commissions.fail_orders = True
    result = await greeter.greet(force=True)

    assert result["greeted"] is True
    assert result["kind"] == FIRST_OF_DAY


async def test_unowned_show_is_never_ordered_from(greeter):
    greeter.commissions.owned_shows = {"someone-elses-show"}
    result = await greeter.greet(force=True)

    assert result["greeted"] is True
    assert greeter.commissions.ordered == []


# =====================================================================
# THE DAILY ORDER
# =====================================================================

async def test_bulletin_is_ordered_daily_without_any_listener(greeter):
    greeter.tracker.reading = (0, 0)
    await greeter.tick()

    assert len(greeter.commissions.ordered) == 1
    assert "bulletin" in greeter.commissions.ordered[0].lower()


async def test_the_daily_order_happens_once(greeter):
    greeter.tracker.reading = (0, 0)
    await greeter.tick()
    await greeter.tick()
    await greeter.tick()

    assert len(greeter.commissions.ordered) == 1


async def test_no_order_before_the_news_hour(greeter):
    greeter.news_hour = 24  # never reached today
    greeter.tracker.reading = (0, 0)
    await greeter.tick()

    assert greeter.commissions.ordered == []


async def test_daily_concept_names_the_date_and_daypart(greeter):
    greeter.tracker.reading = (0, 0)
    await greeter.tick()

    from datetime import datetime
    concept = greeter.commissions.ordered[0]
    assert datetime.now().strftime("%Y") in concept
    assert "Gothenburg" in concept


# =====================================================================
# A BULLETIN LANDING MID-LISTEN
# =====================================================================

async def test_bulletin_landing_while_listening_airs_next_without_a_skip(greeter):
    # The day's first greeting promised a bulletin that was still building.
    await greeter.greet(force=True)
    await _drain_air_task(greeter)
    pending = greeter.commissions.rows[-1]
    assert pending["state"] == "pending"

    # It lands while the listener is still connected.
    pending.update(state="ready", file_path="/music/_programmes/f.mp3",
                   title="Landed", duration_seconds=290.0,
                   loudness_lufs=-16.0, true_peak_dbfs=-1.4)
    greeter._last_listeners = 1
    greeter.tracker.reading = (1, 1)
    await greeter.tick()

    item, position = greeter.planner.inserted[0]
    assert item["title"] == "Landed"
    assert position == 0
    assert greeter.mixer.skips == 0, "twenty minutes in, we don't cut the song"
    assert pending["job_id"] in greeter.commissions.aired
    heads_up = greeter.voice_scheduler.segments[-1].text.lower()
    assert "landed" in heads_up or "up next" in heads_up


async def test_landed_bulletin_waits_for_a_listener(greeter):
    await greeter.greet(force=True)
    await _drain_air_task(greeter)
    greeter.commissions.rows[-1].update(
        state="ready", file_path="/music/_programmes/f.mp3", title="Landed",
        duration_seconds=290.0, loudness_lufs=-16.0, true_peak_dbfs=-1.4)

    greeter.tracker.reading = (0, 0)  # nobody there
    await greeter.tick()

    assert greeter.planner.inserted == [], "an empty room gets no bulletin"


# =====================================================================
# THE WORDS
# =====================================================================

def test_greeting_uses_the_listeners_name():
    text = build_greeting(listener_name="Dan", gap_seconds=3600.0,
                          first_of_day=False, bulletin={"status": "none"},
                          fun_fact=None, hour=20)
    assert "Dan" in text


def test_a_long_absence_is_called_out_in_days():
    text = build_greeting(listener_name="", gap_seconds=7 * 86400.0,
                          first_of_day=False, bulletin={"status": "none"},
                          fun_fact=None, hour=20)
    assert "7" in text and "days" in text


def test_a_first_ever_listener_is_welcomed_not_welcomed_back():
    text = build_greeting(listener_name="", gap_seconds=None,
                          first_of_day=False, bulletin={"status": "none"},
                          fun_fact=None, hour=20)
    assert "back" not in text.lower()


def test_greeting_is_speech_not_markup():
    text = build_greeting(listener_name="Dan", gap_seconds=86400.0,
                          first_of_day=True,
                          bulletin={"status": "ready", "eta_minutes": 0},
                          fun_fact="Station health report: 120 gigabytes of disk to spare.",
                          hour=8)
    assert "#" not in text and "*" not in text and "\n" not in text
