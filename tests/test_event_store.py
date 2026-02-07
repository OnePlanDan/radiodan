"""Tests for EventStore — CRUD, window queries, pub/sub, edge cases."""

import asyncio
import json
import time
from pathlib import Path

import pytest

from bridge.event_store import EventStore


# =========================================================================
# CRUD — start_event
# =========================================================================


async def test_start_event_returns_positive_id(event_store):
    eid = await event_store.start_event("track_play", "music", "Test Track")
    assert eid > 0


async def test_start_event_persists_all_columns(event_store):
    before = time.time()
    eid = await event_store.start_event("track_play", "music", "Test Track")

    async with event_store._db.execute(
        "SELECT * FROM event_log WHERE id = ?", (eid,)
    ) as cursor:
        row = await cursor.fetchone()

    assert row["event_type"] == "track_play"
    assert row["lane"] == "music"
    assert row["title"] == "Test Track"
    assert row["started_at"] >= before
    assert row["ended_at"] is None
    assert row["status"] == "active"
    assert row["created_at"] >= before


async def test_start_event_with_details(event_store):
    details = {"filename": "/music/song.mp3", "artist": "DJ Test"}
    eid = await event_store.start_event(
        "track_play", "music", "Test", details=details
    )

    async with event_store._db.execute(
        "SELECT key, value FROM event_detail WHERE event_id = ?", (eid,)
    ) as cursor:
        rows = {row["key"]: json.loads(row["value"]) async for row in cursor}

    assert rows["filename"] == "/music/song.mp3"
    assert rows["artist"] == "DJ Test"


async def test_start_event_custom_started_at_and_status(event_store):
    ts = 1700000000.0
    eid = await event_store.start_event(
        "voice_segment", "presenter", "Hello",
        started_at=ts, status="scheduled",
    )

    async with event_store._db.execute(
        "SELECT started_at, status FROM event_log WHERE id = ?", (eid,)
    ) as cursor:
        row = await cursor.fetchone()

    assert row["started_at"] == ts
    assert row["status"] == "scheduled"


# =========================================================================
# CRUD — end_event
# =========================================================================


async def test_end_event_sets_ended_at_and_status(event_store):
    eid = await event_store.start_event("track_play", "music", "Song")
    before_end = time.time()
    await event_store.end_event(eid)

    async with event_store._db.execute(
        "SELECT ended_at, status FROM event_log WHERE id = ?", (eid,)
    ) as cursor:
        row = await cursor.fetchone()

    assert row["ended_at"] >= before_end
    assert row["status"] == "completed"


async def test_end_event_with_extra_details(event_store):
    eid = await event_store.start_event(
        "tts_generate", "system", "TTS",
        details={"text": "hello"},
    )
    await event_store.end_event(eid, extra_details={"size_bytes": 44100})

    async with event_store._db.execute(
        "SELECT key, value FROM event_detail WHERE event_id = ?", (eid,)
    ) as cursor:
        rows = {row["key"]: json.loads(row["value"]) async for row in cursor}

    assert rows["text"] == "hello"
    assert rows["size_bytes"] == 44100


async def test_end_event_upserts_existing_detail_key(event_store):
    """INSERT OR REPLACE should overwrite existing detail keys."""
    eid = await event_store.start_event(
        "track_play", "music", "Song",
        details={"status_note": "starting"},
    )
    await event_store.end_event(eid, extra_details={"status_note": "done"})

    async with event_store._db.execute(
        "SELECT value FROM event_detail WHERE event_id = ? AND key = ?",
        (eid, "status_note"),
    ) as cursor:
        row = await cursor.fetchone()

    assert json.loads(row["value"]) == "done"


# =========================================================================
# CRUD — update_event
# =========================================================================


async def test_update_event_title(event_store):
    eid = await event_store.start_event("track_play", "music", "Old Title")
    await event_store.update_event(eid, title="New Title")

    async with event_store._db.execute(
        "SELECT title FROM event_log WHERE id = ?", (eid,)
    ) as cursor:
        row = await cursor.fetchone()

    assert row["title"] == "New Title"


async def test_update_event_status(event_store):
    eid = await event_store.start_event("voice_segment", "presenter", "V", status="scheduled")
    await event_store.update_event(eid, status="active")

    async with event_store._db.execute(
        "SELECT status FROM event_log WHERE id = ?", (eid,)
    ) as cursor:
        row = await cursor.fetchone()

    assert row["status"] == "active"


async def test_update_event_ended_at(event_store):
    eid = await event_store.start_event("track_play", "music", "Song")
    ts = 1700000500.0
    await event_store.update_event(eid, ended_at=ts)

    async with event_store._db.execute(
        "SELECT ended_at FROM event_log WHERE id = ?", (eid,)
    ) as cursor:
        row = await cursor.fetchone()

    assert row["ended_at"] == ts


async def test_update_event_ignores_disallowed_fields(event_store):
    eid = await event_store.start_event("track_play", "music", "Song")
    await event_store.update_event(eid, lane="hacked", event_type="bad")

    async with event_store._db.execute(
        "SELECT lane, event_type FROM event_log WHERE id = ?", (eid,)
    ) as cursor:
        row = await cursor.fetchone()

    assert row["lane"] == "music"
    assert row["event_type"] == "track_play"


async def test_update_event_no_valid_fields_is_noop(event_store):
    """update_event with only disallowed fields should not error or publish."""
    queue = event_store.subscribe()
    eid = await event_store.start_event("track_play", "music", "Song")
    # Drain the start_event notification
    await queue.get()

    await event_store.update_event(eid, lane="bad", event_type="bad")

    # Queue should be empty (no publish for no-op update)
    assert queue.empty()


# =========================================================================
# WINDOW QUERIES
# =========================================================================


async def _seed_events(store, events):
    """Helper: insert events and return their IDs.

    Each event is a dict with keys: event_type, lane, title, started_at,
    optionally ended_at, status, details.
    """
    ids = []
    for e in events:
        eid = await store.start_event(
            e["event_type"], e["lane"], e["title"],
            started_at=e.get("started_at"),
            status=e.get("status", "active"),
            details=e.get("details"),
        )
        if "ended_at" in e:
            # Use raw SQL to set ended_at precisely (end_event uses time.time())
            async with store._lock:
                await store._db.execute(
                    "UPDATE event_log SET ended_at = ?, status = ? WHERE id = ?",
                    (e["ended_at"], e.get("final_status", "completed"), eid),
                )
                await store._db.commit()
        ids.append(eid)
    return ids


async def test_window_returns_events_within_range(event_store):
    await _seed_events(event_store, [
        {"event_type": "track_play", "lane": "music", "title": "In range",
         "started_at": 100.0, "ended_at": 200.0},
    ])
    result = await event_store.get_window(50.0, 250.0)
    assert len(result) == 1
    assert result[0]["title"] == "In range"


async def test_window_includes_events_overlapping_start(event_store):
    """Event that started before window but ends inside it."""
    await _seed_events(event_store, [
        {"event_type": "track_play", "lane": "music", "title": "Overlap start",
         "started_at": 50.0, "ended_at": 150.0},
    ])
    result = await event_store.get_window(100.0, 200.0)
    assert len(result) == 1


async def test_window_includes_events_overlapping_end(event_store):
    """Event that started inside window but ends after it."""
    await _seed_events(event_store, [
        {"event_type": "track_play", "lane": "music", "title": "Overlap end",
         "started_at": 150.0, "ended_at": 250.0},
    ])
    result = await event_store.get_window(100.0, 200.0)
    assert len(result) == 1


async def test_window_includes_events_spanning_entire_window(event_store):
    """Event that started before and ends after the window."""
    await _seed_events(event_store, [
        {"event_type": "track_play", "lane": "music", "title": "Spanning",
         "started_at": 50.0, "ended_at": 250.0},
    ])
    result = await event_store.get_window(100.0, 200.0)
    assert len(result) == 1


async def test_window_includes_open_ended_events(event_store):
    """Events with ended_at IS NULL should be included (still active)."""
    await _seed_events(event_store, [
        {"event_type": "track_play", "lane": "music", "title": "Still playing",
         "started_at": 150.0},
    ])
    result = await event_store.get_window(100.0, 200.0)
    assert len(result) == 1
    assert result[0]["ended_at"] is None


async def test_window_excludes_events_fully_before(event_store):
    await _seed_events(event_store, [
        {"event_type": "track_play", "lane": "music", "title": "Before",
         "started_at": 10.0, "ended_at": 50.0},
    ])
    result = await event_store.get_window(100.0, 200.0)
    assert len(result) == 0


async def test_window_excludes_events_fully_after(event_store):
    await _seed_events(event_store, [
        {"event_type": "track_play", "lane": "music", "title": "After",
         "started_at": 300.0, "ended_at": 400.0},
    ])
    result = await event_store.get_window(100.0, 200.0)
    assert len(result) == 0


async def test_window_lane_filter_single(event_store):
    await _seed_events(event_store, [
        {"event_type": "track_play", "lane": "music", "title": "Music",
         "started_at": 100.0, "ended_at": 200.0},
        {"event_type": "tts_generate", "lane": "system", "title": "System",
         "started_at": 100.0, "ended_at": 200.0},
    ])
    result = await event_store.get_window(50.0, 250.0, lanes=["music"])
    assert len(result) == 1
    assert result[0]["lane"] == "music"


async def test_window_lane_filter_multiple(event_store):
    await _seed_events(event_store, [
        {"event_type": "track_play", "lane": "music", "title": "Music",
         "started_at": 100.0, "ended_at": 200.0},
        {"event_type": "voice_segment", "lane": "presenter", "title": "Voice",
         "started_at": 100.0, "ended_at": 200.0},
        {"event_type": "tts_generate", "lane": "system", "title": "TTS",
         "started_at": 100.0, "ended_at": 200.0},
    ])
    result = await event_store.get_window(50.0, 250.0, lanes=["music", "presenter"])
    assert len(result) == 2
    lanes = {e["lane"] for e in result}
    assert lanes == {"music", "presenter"}


async def test_window_includes_batch_loaded_details(event_store):
    await _seed_events(event_store, [
        {"event_type": "track_play", "lane": "music", "title": "Song",
         "started_at": 100.0, "ended_at": 200.0,
         "details": {"artist": "TestArtist", "filename": "song.mp3"}},
    ])
    result = await event_store.get_window(50.0, 250.0)
    assert result[0]["details"]["artist"] == "TestArtist"
    assert result[0]["details"]["filename"] == "song.mp3"


async def test_window_ordered_by_started_at(event_store):
    await _seed_events(event_store, [
        {"event_type": "a", "lane": "music", "title": "Third",
         "started_at": 300.0, "ended_at": 400.0},
        {"event_type": "a", "lane": "music", "title": "First",
         "started_at": 100.0, "ended_at": 200.0},
        {"event_type": "a", "lane": "music", "title": "Second",
         "started_at": 200.0, "ended_at": 300.0},
    ])
    result = await event_store.get_window(0.0, 500.0)
    titles = [e["title"] for e in result]
    assert titles == ["First", "Second", "Third"]


async def test_window_empty_result(event_store):
    result = await event_store.get_window(100.0, 200.0)
    assert result == []


# =========================================================================
# PUB/SUB
# =========================================================================


async def test_subscribe_returns_queue(event_store):
    queue = event_store.subscribe()
    assert isinstance(queue, asyncio.Queue)


async def test_start_event_publishes(event_store):
    queue = event_store.subscribe()
    eid = await event_store.start_event("track_play", "music", "Song")

    msg = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert msg["action"] == "start"
    assert msg["event"]["id"] == eid
    assert msg["event"]["event_type"] == "track_play"


async def test_end_event_publishes(event_store):
    eid = await event_store.start_event("track_play", "music", "Song")
    queue = event_store.subscribe()

    await event_store.end_event(eid)
    msg = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert msg["action"] == "end"
    assert msg["event"]["id"] == eid
    assert msg["event"]["status"] == "completed"


async def test_update_event_publishes(event_store):
    eid = await event_store.start_event("track_play", "music", "Old")
    queue = event_store.subscribe()

    await event_store.update_event(eid, title="New")
    msg = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert msg["action"] == "update"
    assert msg["event"]["id"] == eid
    assert msg["event"]["title"] == "New"


async def test_multiple_subscribers_all_receive(event_store):
    q1 = event_store.subscribe()
    q2 = event_store.subscribe()

    await event_store.start_event("track_play", "music", "Song")

    msg1 = await asyncio.wait_for(q1.get(), timeout=1.0)
    msg2 = await asyncio.wait_for(q2.get(), timeout=1.0)
    assert msg1["action"] == "start"
    assert msg2["action"] == "start"


async def test_unsubscribe_stops_delivery(event_store):
    queue = event_store.subscribe()
    event_store.unsubscribe(queue)

    await event_store.start_event("track_play", "music", "Song")
    assert queue.empty()


async def test_unsubscribe_nonexistent_is_safe(event_store):
    """Unsubscribing a queue that was never subscribed should not error."""
    rogue_queue = asyncio.Queue()
    event_store.unsubscribe(rogue_queue)  # Should not raise


async def test_backpressure_drops_oldest(event_store):
    """When queue is full (256), publishing drops the oldest message."""
    queue = event_store.subscribe()

    # Fill the queue to capacity (256)
    for i in range(256):
        await event_store.start_event("fill", "test", f"Event {i}")

    assert queue.full()

    # One more publish should succeed by dropping oldest
    await event_store.start_event("overflow", "test", "Event 256")

    # Queue should still be full at 256
    assert queue.qsize() == 256

    # The first message should have been dropped — first available should be Event 1
    msg = await queue.get()
    assert msg["event"]["title"] == "Event 1"


# =========================================================================
# EDGE CASES
# =========================================================================


async def test_start_event_on_unopened_store():
    """Operations on a store that was never opened return safely."""
    store = EventStore(db_path=Path(":memory:"))
    eid = await store.start_event("track_play", "music", "Song")
    assert eid == -1


async def test_end_event_negative_id_is_noop(event_store):
    """end_event with id=-1 should be a no-op."""
    await event_store.end_event(-1)  # Should not raise


async def test_update_event_negative_id_is_noop(event_store):
    """update_event with id=-1 should be a no-op."""
    await event_store.update_event(-1, title="Nope")  # Should not raise


async def test_get_window_on_unopened_store():
    store = EventStore(db_path=Path(":memory:"))
    result = await store.get_window(0.0, 100.0)
    assert result == []


async def test_operations_after_close(event_store):
    """Operations after close() should return safely, not crash."""
    await event_store.close()

    assert await event_store.start_event("x", "y", "z") == -1
    await event_store.end_event(1)  # no-op
    await event_store.update_event(1, title="z")  # no-op
    assert await event_store.get_window(0.0, 100.0) == []
