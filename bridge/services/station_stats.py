"""
Station statistics — the numbers the station likes to brag about.

One snapshot() gathering everything worth reporting live: uptime, disk, the
library, today's plays, voice output, listeners, bulletins. Used two ways:
the GET /api/stats endpoint, and the greeter dropping one true fact into a
welcome ("while you were away I played 214 songs to an empty room").

Every metric is best-effort and independent: a missing table or a locked DB
drops that number from the snapshot instead of raising. The stats service
must never be the reason anything else fails.

Timestamp formats differ per table and that is handled here, in one place:
`playlist_history.played_at` and `music_library.last_played_at` are ISO-8601
UTC strings; `event_log`, `listener_samples`, `commissions` use epoch floats.
"""

import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _utc_iso(ts: float) -> str:
    """Epoch → the ISO-8601 UTC form playlist_history stores, for comparison."""
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _local_midnight_ts() -> float:
    now = datetime.now()
    return datetime(now.year, now.month, now.day).timestamp()


class StationStats:
    """Read-only reporter over the station database and host."""

    def __init__(self, db_path, music_dir: Path, started_at: float | None = None):
        self.db_path = db_path
        self.music_dir = Path(music_dir)
        self.started_at = started_at or time.time()
        self._db = None

    async def start(self) -> None:
        import aiosqlite

        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA busy_timeout = 5000")
        await self._db.execute("PRAGMA query_only = ON")

    async def stop(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    async def _one(self, sql: str, params: tuple = ()):
        """A single value, or None if the query can't run. Isolation per metric."""
        try:
            async with self._db.execute(sql, params) as cursor:
                row = await cursor.fetchone()
            return row[0] if row else None
        except Exception:
            return None

    async def songs_since(self, ts: float) -> int | None:
        """How many songs have played since an epoch moment."""
        return await self._one(
            "SELECT COUNT(*) FROM playlist_history WHERE played_at >= ?",
            (_utc_iso(ts),),
        )

    async def snapshot(self) -> dict:
        midnight = _local_midnight_ts()
        now = time.time()

        snap: dict = {
            "uptime_hours": round((now - self.started_at) / 3600, 1),
            "generated_at": now,
        }

        try:
            usage = shutil.disk_usage(self.music_dir)
            snap["disk_free_gb"] = round(usage.free / 1e9, 1)
            snap["disk_total_gb"] = round(usage.total / 1e9, 1)
            snap["disk_used_percent"] = round(100 * (1 - usage.free / usage.total), 1)
        except Exception:
            pass

        try:
            snap["database_mb"] = round(Path(self.db_path).stat().st_size / 1e6, 1)
        except Exception:
            pass

        snap["library_tracks"] = await self._one("SELECT COUNT(*) FROM music_library")
        snap["library_hours"] = _maybe_round(
            await self._one("SELECT SUM(duration_seconds) / 3600.0 FROM music_library"), 1
        )
        snap["songs_played_total"] = await self._one("SELECT COUNT(*) FROM playlist_history")
        snap["songs_played_today"] = await self.songs_since(midnight)
        snap["starred_tracks"] = await self._one(
            "SELECT COUNT(DISTINCT file_path) FROM track_stars"
        )

        snap["voice_segments_today"] = await self._one(
            "SELECT COUNT(*) FROM event_log "
            "WHERE event_type = 'voice_segment' AND started_at >= ?",
            (midnight,),
        )

        minutes = await self._one(
            "SELECT COUNT(*) FROM listener_samples WHERE listeners > 0 AND sampled_at >= ?",
            (midnight,),
        )
        # One sample a minute, so a count of present-samples is minutes listened.
        snap["listener_minutes_today"] = minutes
        last_heard = await self._one(
            "SELECT MAX(sampled_at) FROM listener_samples WHERE listeners > 0"
        )
        snap["listener_last_heard_at"] = last_heard

        snap["bulletins_aired"] = await self._one(
            "SELECT COUNT(*) FROM commissions WHERE state = 'aired'"
        )
        snap["bulletins_ready"] = await self._one(
            "SELECT COUNT(*) FROM commissions WHERE state = 'ready'"
        )
        snap["greetings_total"] = await self._one(
            "SELECT COUNT(*) FROM greeter_log WHERE kind IN ('arrival', 'first_of_day')"
        )

        return {k: v for k, v in snap.items() if v is not None}


def _maybe_round(value, digits: int):
    return round(value, digits) if value is not None else None
