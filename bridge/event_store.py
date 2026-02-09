"""
RadioDan Event Store

Persists timeline events to SQLite and publishes them to SSE subscribers
via async queues. Each event represents an activity (track play, TTS
generation, LLM request, voice segment) displayed on the DAW-like timeline.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

EVENT_STORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL,
    lane        TEXT NOT NULL,
    title       TEXT NOT NULL,
    started_at  REAL NOT NULL,
    ended_at    REAL,
    status      TEXT DEFAULT 'active',
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS event_detail (
    event_id    INTEGER NOT NULL REFERENCES event_log(id),
    key         TEXT NOT NULL,
    value       TEXT,
    PRIMARY KEY (event_id, key)
);

CREATE INDEX IF NOT EXISTS idx_event_log_started ON event_log(started_at);
CREATE INDEX IF NOT EXISTS idx_event_log_lane ON event_log(lane);
CREATE INDEX IF NOT EXISTS idx_event_log_status ON event_log(status);
"""


class EventStore:
    """SQLite-backed event store with pub/sub for live SSE streaming."""

    def __init__(self, db_path: Path):
        self._db: aiosqlite.Connection | None = None
        self._db_path = db_path
        self._subscribers: list[asyncio.Queue] = []
        self._lock = asyncio.Lock()
        self._last_music_z_stagger: int = 0

    async def open(self) -> None:
        """Open database and create tables.

        Any events left 'active' or 'scheduled' from a previous run are
        orphans (the process died before ending them). Close them now so
        the timeline doesn't show phantom overlapping events.
        """
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(EVENT_STORE_SCHEMA)
        await self._db.commit()

        # Close orphaned active events from previous process — set ended_at to
        # started_at so they collapse to zero-width on the timeline rather
        # than stretching all the way to "now" and overlapping current events
        cursor_active = await self._db.execute(
            "UPDATE event_log SET ended_at = COALESCE(ended_at, started_at), status = 'completed' "
            "WHERE status = 'active'",
        )
        # Mark orphaned scheduled events as cancelled
        cursor_scheduled = await self._db.execute(
            "UPDATE event_log SET ended_at = COALESCE(ended_at, started_at), status = 'cancelled' "
            "WHERE status = 'scheduled'",
        )
        await self._db.commit()
        orphaned = (cursor_active.rowcount or 0) + (cursor_scheduled.rowcount or 0)
        if orphaned:
            logger.info(
                f"Closed {cursor_active.rowcount} orphaned active and "
                f"{cursor_scheduled.rowcount} orphaned scheduled events from previous run"
            )

        # Recover last music z_stagger from DB for stable alternation
        async with self._db.execute(
            "SELECT d.value FROM event_detail d "
            "JOIN event_log e ON d.event_id = e.id "
            "WHERE e.lane = 'music' AND d.key = 'z_stagger' "
            "ORDER BY e.id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            if row:
                try:
                    self._last_music_z_stagger = int(json.loads(row["value"]))
                except (ValueError, TypeError, json.JSONDecodeError):
                    pass
        logger.info(f"Last music z_stagger: {self._last_music_z_stagger}")

        logger.info("Event store opened")

    async def close(self) -> None:
        """Close database connection."""
        if self._db:
            await self._db.close()
            self._db = None
            logger.info("Event store closed")

    async def start_event(
        self,
        event_type: str,
        lane: str,
        title: str,
        details: dict | None = None,
        status: str = "active",
        started_at: float | None = None,
    ) -> int:
        """Insert a new event and publish to subscribers.

        Returns the event id.
        """
        if not self._db:
            return -1

        now = time.time()
        ts = started_at or now

        async with self._lock:
            cursor = await self._db.execute(
                "INSERT INTO event_log (event_type, lane, title, started_at, ended_at, status, created_at) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?)",
                (event_type, lane, title, ts, status, now),
            )
            event_id = cursor.lastrowid

            if details:
                for key, value in details.items():
                    await self._db.execute(
                        "INSERT INTO event_detail (event_id, key, value) VALUES (?, ?, ?)",
                        (event_id, key, json.dumps(value)),
                    )

            await self._db.commit()

        # Track z_stagger for stable music lane alternation
        if lane == "music" and details and "z_stagger" in details:
            self._last_music_z_stagger = int(details["z_stagger"])

        event = {
            "id": event_id,
            "event_type": event_type,
            "lane": lane,
            "title": title,
            "started_at": ts,
            "ended_at": None,
            "status": status,
            "created_at": now,
            "details": details or {},
        }
        self._publish({"action": "start", "event": event})
        return event_id

    @property
    def last_music_z_stagger(self) -> int:
        """Last z_stagger value used for a music event (0 or 1)."""
        return self._last_music_z_stagger

    async def get_last_music_filename(self) -> str | None:
        """Return the filename from the most recent music event, or None."""
        if not self._db:
            return None
        async with self._db.execute(
            "SELECT d.value FROM event_detail d "
            "JOIN event_log e ON d.event_id = e.id "
            "WHERE e.lane = 'music' AND d.key = 'filename' "
            "ORDER BY e.id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            if row:
                try:
                    return json.loads(row["value"])
                except (json.JSONDecodeError, TypeError):
                    return None
        return None

    async def get_last_music_event_id(self) -> int | None:
        """Return the id of the most recent music event, or None."""
        if not self._db:
            return None
        async with self._db.execute(
            "SELECT id FROM event_log "
            "WHERE lane = 'music' ORDER BY id DESC LIMIT 1"
        ) as cur:
            row = await cur.fetchone()
            return row["id"] if row else None

    async def end_event(
        self,
        event_id: int,
        status: str = "completed",
        extra_details: dict | None = None,
    ) -> None:
        """Mark an event as ended."""
        if not self._db or event_id < 0:
            return

        now = time.time()
        async with self._lock:
            await self._db.execute(
                "UPDATE event_log SET ended_at = ?, status = ? WHERE id = ?",
                (now, status, event_id),
            )
            if extra_details:
                for key, value in extra_details.items():
                    await self._db.execute(
                        "INSERT OR REPLACE INTO event_detail (event_id, key, value) VALUES (?, ?, ?)",
                        (event_id, key, json.dumps(value)),
                    )
            await self._db.commit()

        self._publish({
            "action": "end",
            "event": {"id": event_id, "ended_at": now, "status": status},
        })

    async def update_event(self, event_id: int, **kwargs) -> None:
        """Update event fields (title, status, ended_at)."""
        if not self._db or event_id < 0:
            return

        allowed = {"title", "status", "ended_at", "started_at"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return

        async with self._lock:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [event_id]
            await self._db.execute(
                f"UPDATE event_log SET {set_clause} WHERE id = ?",
                values,
            )
            await self._db.commit()

        self._publish({
            "action": "update",
            "event": {"id": event_id, **updates},
        })

    async def get_window(
        self,
        start_ts: float,
        end_ts: float,
        lanes: list[str] | None = None,
    ) -> list[dict]:
        """Query events overlapping [start_ts, end_ts].

        An event overlaps the window if:
          - it started before the window ends, AND
          - it hasn't ended OR ended after the window starts
        """
        if not self._db:
            return []

        # Build query for events overlapping the time window
        query = (
            "SELECT id, event_type, lane, title, started_at, ended_at, status, created_at "
            "FROM event_log "
            "WHERE started_at <= ? AND (ended_at IS NULL OR ended_at >= ?)"
        )
        params: list = [end_ts, start_ts]

        if lanes:
            placeholders = ",".join("?" for _ in lanes)
            query += f" AND lane IN ({placeholders})"
            params.extend(lanes)

        query += " ORDER BY started_at"

        events = []
        async with self._db.execute(query, params) as cursor:
            async for row in cursor:
                event = {
                    "id": row["id"],
                    "event_type": row["event_type"],
                    "lane": row["lane"],
                    "title": row["title"],
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "details": {},
                }
                events.append(event)

        # Batch-load details for all events
        if events:
            event_ids = [e["id"] for e in events]
            placeholders = ",".join("?" for _ in event_ids)
            detail_map: dict[int, dict] = {eid: {} for eid in event_ids}
            async with self._db.execute(
                f"SELECT event_id, key, value FROM event_detail WHERE event_id IN ({placeholders})",
                event_ids,
            ) as cursor:
                async for row in cursor:
                    try:
                        detail_map[row["event_id"]][row["key"]] = json.loads(row["value"])
                    except (json.JSONDecodeError, KeyError):
                        detail_map[row["event_id"]][row["key"]] = row["value"]

            for event in events:
                event["details"] = detail_map.get(event["id"], {})

        return events

    def subscribe(self) -> asyncio.Queue:
        """Return a queue that receives all published event messages."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """Remove a subscriber queue."""
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    def _publish(self, message: dict) -> None:
        """Push a message to all subscriber queues (non-blocking)."""
        for queue in self._subscribers:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Drop oldest message to prevent backpressure blocking
                try:
                    queue.get_nowait()
                    queue.put_nowait(message)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
