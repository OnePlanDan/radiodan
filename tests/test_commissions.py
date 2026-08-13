"""Tests for commissioning programme episodes.

An episode is ordered against a brief, arrives ~20 minutes later, and is then
scheduled like a song. The station's priority is that music never stops, so a
commission is speculative: a late or failed episode costs its build and nothing
else, and nothing airs until it is on local disk and measured.
"""

import time
from pathlib import Path

import pytest

from bridge.services.audiosegment import AudioSegmentError
from bridge.services.commissions import (
    AIRED,
    FAILED_STATE,
    PENDING,
    READY,
    CommissionService,
)


class FakeClient:
    """Stands in for AudioSegmentClient."""

    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.audio = b"ID3fake"
        self.produced: list[tuple] = []
        self.requeued: list[str] = []
        self.download_error: Exception | None = None
        self.job_error: Exception | None = None
        self._n = 0

    async def produce(self, show, concept, location=None, weight=None, context_mode=None):
        self._n += 1
        job_id = f"job-{self._n:03d}"
        self.produced.append((show, concept, location, weight, context_mode))
        self.jobs[job_id] = {"job_id": job_id, "status": "queued", "show_name": show}
        return {"job_id": job_id, "status": "queued", "position": 0}

    async def job(self, job_id):
        if self.job_error:
            raise self.job_error
        return self.jobs[job_id]

    async def requeue(self, job_id):
        self.requeued.append(job_id)
        self.jobs[job_id]["status"] = "queued"
        return {"status": "queued"}

    async def download_audio(self, job_id, dest):
        if self.download_error:
            raise self.download_error
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.audio)
        return dest


@pytest.fixture
async def svc(tmp_path, monkeypatch):
    client = FakeClient()
    s = CommissionService(
        client=client,
        db_path=tmp_path / "radiodan.db",
        programme_dir=tmp_path / "_programmes",
        poll_interval=15.0,
        owned_shows=["lani-viv", "bobs-boat"],
    )
    # Measurement and duration shell out to ffmpeg/ffprobe; stub them so the
    # tests exercise the flow, not the encoders.
    monkeypatch.setattr("bridge.services.commissions.measure_file",
                        _fake(( -15.2, -1.8)))
    monkeypatch.setattr("bridge.services.commissions._duration_of", _fake(612.0))
    await s.start()
    s._task.cancel()  # tests drive polling explicitly
    yield s
    await s.stop()


def _fake(value):
    async def _f(*a, **k):
        return value
    return _f


# =====================================================================
# ORDERING
# =====================================================================

async def test_commission_records_a_pending_row(svc):
    row = await svc.commission("lani-viv", "A ship not on the manifest")
    assert row["state"] == PENDING
    assert row["show"] == "lani-viv"
    assert row["concept"] == "A ship not on the manifest"
    assert row["requested_at"] > 0


async def test_commission_passes_the_brief_through(svc):
    await svc.commission("lani-viv", "concept", location="Gothenburg", weight=6)
    assert svc.client.produced[-1][:4] == ("lani-viv", "concept", "Gothenburg", 6)


async def test_pending_lists_outstanding_work(svc):
    await svc.commission("lani-viv", "one")
    await svc.commission("bobs-boat", "two")
    assert len(await svc.pending()) == 2


# =====================================================================
# COLLECTING
# =====================================================================

async def test_still_producing_is_not_collected(svc):
    row = await svc.commission("lani-viv", "c")
    svc.client.jobs[row["job_id"]]["status"] = "synthesizing"

    assert await svc.poll_once() == 0
    assert (await svc.get(row["job_id"]))["state"] == PENDING


async def test_remote_status_is_tracked_while_producing(svc):
    row = await svc.commission("lani-viv", "c")
    svc.client.jobs[row["job_id"]]["status"] = "scripting"
    await svc.poll_once()
    assert (await svc.get(row["job_id"]))["remote_status"] == "scripting"


async def test_completed_episode_is_downloaded_and_measured(svc, tmp_path):
    row = await svc.commission("lani-viv", "c")
    svc.client.jobs[row["job_id"]].update({"status": "completed", "script_title": "The Hum"})

    assert await svc.poll_once() == 1
    got = await svc.get(row["job_id"])
    assert got["state"] == READY
    assert got["title"] == "The Hum"
    assert got["loudness_lufs"] == -15.2
    assert got["true_peak_dbfs"] == -1.8
    assert got["duration_seconds"] == 612.0
    assert Path(got["file_path"]).exists()


async def test_episode_lands_in_the_programme_directory(svc, tmp_path):
    row = await svc.commission("lani-viv", "c")
    svc.client.jobs[row["job_id"]]["status"] = "completed"
    await svc.poll_once()

    got = await svc.get(row["job_id"])
    assert Path(got["file_path"]).parent == tmp_path / "_programmes"


async def test_a_failed_download_leaves_it_pending_for_retry(svc):
    """The service being flaky must not burn the commission."""
    row = await svc.commission("lani-viv", "c")
    svc.client.jobs[row["job_id"]]["status"] = "completed"
    svc.client.download_error = AudioSegmentError("connection reset")

    assert await svc.poll_once() == 0
    assert (await svc.get(row["job_id"]))["state"] == PENDING

    svc.client.download_error = None
    assert await svc.poll_once() == 1
    assert (await svc.get(row["job_id"]))["state"] == READY


async def test_unmeasurable_audio_is_never_scheduled(svc, monkeypatch):
    """Measurement on receipt is the delivery-spec check. Failing it means the
    file does not go on air."""
    monkeypatch.setattr("bridge.services.commissions.measure_file", _fake(None))
    row = await svc.commission("lani-viv", "c")
    svc.client.jobs[row["job_id"]]["status"] = "completed"

    await svc.poll_once()
    got = await svc.get(row["job_id"])
    assert got["state"] == FAILED_STATE
    assert "measured" in got["error"]
    assert await svc.ready() == []


async def test_unreachable_service_does_not_lose_the_commission(svc):
    row = await svc.commission("lani-viv", "c")
    svc.client.job_error = AudioSegmentError("service down")

    assert await svc.poll_once() == 0
    assert (await svc.get(row["job_id"]))["state"] == PENDING


# =====================================================================
# FAILURE HANDLING
# =====================================================================

async def test_a_failure_is_requeued_once(svc):
    """Requeue resumes at `scripted`, so a retry does not repeat scriptwriting."""
    row = await svc.commission("lani-viv", "c")
    job_id = row["job_id"]
    svc.client.jobs[job_id].update({"status": "failed", "error": "tts timeout"})

    await svc.poll_once()
    assert svc.client.requeued == [job_id]
    assert (await svc.get(job_id))["state"] == PENDING, "still outstanding after a retry"


async def test_a_second_failure_gives_up(svc):
    row = await svc.commission("lani-viv", "c")
    job_id = row["job_id"]
    svc.client.jobs[job_id].update({"status": "failed", "error": "tts timeout"})

    await svc.poll_once()                       # requeues
    svc.client.jobs[job_id]["status"] = "failed"
    await svc.poll_once()                       # gives up

    got = await svc.get(job_id)
    assert got["state"] == FAILED_STATE
    assert "tts timeout" in got["error"]
    assert len(svc.client.requeued) == 1


async def test_auto_requeue_can_be_disabled(svc):
    svc.auto_requeue = False
    row = await svc.commission("lani-viv", "c")
    svc.client.jobs[row["job_id"]].update({"status": "failed", "error": "boom"})

    await svc.poll_once()
    assert svc.client.requeued == []
    assert (await svc.get(row["job_id"]))["state"] == FAILED_STATE


async def test_one_bad_commission_does_not_block_the_others(svc):
    good = await svc.commission("lani-viv", "good")
    bad = await svc.commission("bobs-boat", "bad")
    svc.client.jobs[good["job_id"]]["status"] = "completed"
    svc.client.jobs[bad["job_id"]] = {"status": "completed"}  # missing job_id etc.

    # The bad one raises inside _collect; the good one must still land.
    await svc.poll_once()
    assert (await svc.get(good["job_id"]))["state"] == READY


# =====================================================================
# SCHEDULING
# =====================================================================

async def test_ready_episodes_are_offered_oldest_first(svc):
    for concept in ("first", "second"):
        row = await svc.commission("lani-viv", concept)
        svc.client.jobs[row["job_id"]]["status"] = "completed"
        await svc.poll_once()
        time.sleep(0.01)

    assert [r["concept"] for r in await svc.ready()] == ["first", "second"]


async def test_queue_item_has_the_same_shape_as_a_song(svc):
    """'Just a block to be scheduled, like a song' — same keys, so the planner
    needs no special case."""
    row = await svc.commission("lani-viv", "A ship not on the manifest")
    svc.client.jobs[row["job_id"]].update({"status": "completed", "script_title": "The Hum"})
    await svc.poll_once()

    item = svc.to_queue_item((await svc.ready())[0])
    for key in ("file_path", "artist", "title", "duration_seconds",
                "loudness_lufs", "true_peak_dbfs"):
        assert key in item, key
    assert item["artist"] == "lani-viv"
    assert item["title"] == "The Hum"
    assert item["genre"] == "programme"


async def test_queue_item_carries_loudness_so_it_airs_at_station_level(svc):
    """The same per-track gain music gets — no separate path for programmes."""
    row = await svc.commission("lani-viv", "c")
    svc.client.jobs[row["job_id"]]["status"] = "completed"
    await svc.poll_once()

    item = svc.to_queue_item((await svc.ready())[0])
    from bridge.audio.loudness import gain_for
    gain = gain_for(item["loudness_lufs"], target_lufs=-16.0, assumed_lufs=-11.0,
                    max_boost_db=6.0, max_cut_db=30.0,
                    true_peak_dbfs=item["true_peak_dbfs"])
    assert gain == pytest.approx(-0.8, abs=0.01)


async def test_falls_back_to_the_concept_when_untitled(svc):
    row = await svc.commission("lani-viv", "A ship not on the manifest")
    svc.client.jobs[row["job_id"]]["status"] = "completed"
    await svc.poll_once()

    item = svc.to_queue_item((await svc.ready())[0])
    assert item["title"] == "A ship not on the manifest"


async def test_airing_takes_it_out_of_the_ready_pool(svc):
    row = await svc.commission("lani-viv", "c")
    svc.client.jobs[row["job_id"]]["status"] = "completed"
    await svc.poll_once()

    await svc.mark_aired(row["job_id"])
    assert await svc.ready() == []
    assert (await svc.get(row["job_id"]))["state"] == AIRED
    assert (await svc.get(row["job_id"]))["aired_at"] > 0


async def test_poll_interval_has_a_floor(tmp_path):
    s = CommissionService(client=FakeClient(), db_path=tmp_path / "db",
                          programme_dir=tmp_path / "p", poll_interval=1.0)
    assert s.poll_interval >= 15.0
