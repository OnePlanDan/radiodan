"""
Listener presence sampling.

The station needs to know whether anyone is actually there. Producing a
commissioned episode costs roughly 20 minutes of build for 7 minutes of audio,
so pushing episodes out to an empty stream is the most expensive thing the
system can do. And the opposite matters too: some things (a morning news
bulletin) should exist whether or not anyone is listening.

Neither question is answerable without a history of when someone was connected,
and presence history cannot be backfilled — an hour not recorded is an hour we
can never learn the pattern from. Hence this samples from day one, ahead of any
decision about how the schedule will use it.

Icecast reports a listener count on /status-json.xsl without authentication.
That is a count, not identities: two devices look like two listeners, and a
browser that leaves a tab open looks like a listener who never left. Good enough
to answer "is anyone there" and "which hours does someone usually turn up".
"""

import asyncio
import logging
import time

import aiohttp

logger = logging.getLogger(__name__)

LISTENER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS listener_samples (
    sampled_at REAL PRIMARY KEY,
    listeners  INTEGER NOT NULL,
    peak       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_listener_samples_at ON listener_samples(sampled_at);
"""

# One sample a minute is 1 440 rows a day — trivial to store, and fine-grained
# enough for hour-of-day patterns.
DEFAULT_INTERVAL = 60.0

# The DB runs in journal_mode=delete with no busy timeout, so a second writer
# can collide with the planner. One small write a minute makes that rare, and a
# few seconds of waiting resolves it.
_BUSY_TIMEOUT_MS = 5000


class ListenerTracker:
    """Samples the Icecast listener count and answers questions about presence."""

    def __init__(
        self,
        db_path,
        status_url: str,
        interval: float = DEFAULT_INTERVAL,
    ):
        self.db_path = db_path
        self.status_url = status_url
        self.interval = max(5.0, interval)
        self._db = None
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self.samples_taken = 0
        self.read_failures = 0
        self._last_listeners: int | None = None

    # =====================================================================
    # LIFECYCLE
    # =====================================================================

    async def start(self) -> None:
        import aiosqlite

        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        await self._db.executescript(LISTENER_SCHEMA_SQL)
        await self._db.commit()
        self._session = aiohttp.ClientSession()
        self._task = asyncio.create_task(self._run())
        logger.info(
            f"Listener tracking started (every {int(self.interval)}s from {self.status_url})"
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._session:
            await self._session.close()
            self._session = None
        if self._db:
            await self._db.close()
            self._db = None

    async def _run(self) -> None:
        while True:
            try:
                await self.sample_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never let presence sampling take the station down with it.
                logger.exception("Listener sample failed")
            await asyncio.sleep(self.interval)

    # =====================================================================
    # SAMPLING
    # =====================================================================

    async def read_icecast(self) -> tuple[int, int | None] | None:
        """Current listener count and session peak, or None if unreadable."""
        if self._session is None:
            return None
        try:
            async with self._session.get(
                self.status_url, timeout=aiohttp.ClientTimeout(total=8)
            ) as response:
                if response.status != 200:
                    return None
                payload = await response.json(content_type=None)
        except Exception:
            return None

        source = (payload.get("icestats") or {}).get("source")
        # With no source connected Icecast omits the key; with several mounts it
        # becomes a list. Neither should look like "nobody is listening".
        if isinstance(source, list):
            source = source[0] if source else None
        if not isinstance(source, dict):
            return None
        try:
            return int(source.get("listeners", 0)), _maybe_int(source.get("listener_peak"))
        except (TypeError, ValueError):
            return None

    async def sample_once(self) -> int | None:
        """Take and store one sample. Returns the listener count, or None."""
        reading = await self.read_icecast()
        if reading is None:
            self.read_failures += 1
            return None

        listeners, peak = reading
        await self._db.execute(
            "INSERT OR REPLACE INTO listener_samples (sampled_at, listeners, peak) "
            "VALUES (?, ?, ?)",
            (time.time(), listeners, peak),
        )
        await self._db.commit()
        self.samples_taken += 1

        # Arrivals and departures are worth a log line; a steady count is not.
        if self._last_listeners is not None and listeners != self._last_listeners:
            if listeners > self._last_listeners:
                logger.info(f"Listener connected ({listeners} now listening)")
            elif listeners == 0:
                logger.info("Last listener disconnected")
        self._last_listeners = listeners
        return listeners

    # =====================================================================
    # QUESTIONS
    # =====================================================================

    async def presence(self, pattern_days: int = 14) -> dict:
        """What a scheduler needs: anyone now, and which hours they usually appear."""
        now = time.time()

        async with self._db.execute(
            "SELECT sampled_at, listeners, peak FROM listener_samples "
            "ORDER BY sampled_at DESC LIMIT 1"
        ) as cursor:
            latest = await cursor.fetchone()

        async with self._db.execute(
            "SELECT MAX(sampled_at) FROM listener_samples WHERE listeners > 0"
        ) as cursor:
            row = await cursor.fetchone()
        last_heard = row[0] if row and row[0] else None

        minutes = {}
        for label, seconds in (("today", 86400), ("last_7_days", 7 * 86400)):
            async with self._db.execute(
                "SELECT COUNT(*) present, MAX(listeners) peak FROM listener_samples "
                "WHERE sampled_at > ? AND listeners > 0",
                (now - seconds,),
            ) as cursor:
                r = await cursor.fetchone()
            # Each sample stands for one interval, so the count converts to time.
            minutes[label] = {
                "minutes_with_listeners": round((r["present"] or 0) * self.interval / 60, 1),
                "peak_listeners": r["peak"] or 0,
            }

        # Hour-of-day pattern: on how many distinct days did someone listen during
        # this hour. Answers "is 07:00 usually a listening hour?"
        async with self._db.execute(
            "SELECT CAST(strftime('%H', sampled_at, 'unixepoch', 'localtime') AS INTEGER) hour, "
            "COUNT(DISTINCT date(sampled_at, 'unixepoch', 'localtime')) days_present "
            "FROM listener_samples WHERE sampled_at > ? AND listeners > 0 "
            "GROUP BY hour ORDER BY hour",
            (now - pattern_days * 86400,),
        ) as cursor:
            by_hour = {r["hour"]: r["days_present"] async for r in cursor}

        async with self._db.execute(
            "SELECT COUNT(DISTINCT date(sampled_at, 'unixepoch', 'localtime')) days, "
            "MIN(sampled_at) first, COUNT(*) total FROM listener_samples WHERE sampled_at > ?",
            (now - pattern_days * 86400,),
        ) as cursor:
            span = await cursor.fetchone()

        listeners_now = latest["listeners"] if latest else 0
        return {
            "now": {
                "listeners": listeners_now,
                "listening": listeners_now > 0,
                "sampled_at": latest["sampled_at"] if latest else None,
            },
            "last_heard_at": last_heard,
            "silent_for_seconds": round(now - last_heard, 1) if last_heard else None,
            **minutes,
            "typical_hours": [
                {"hour": h, "days_present": by_hour.get(h, 0),
                 "of_days_observed": span["days"] or 0}
                for h in range(24)
            ],
            "sampling": {
                "interval_seconds": self.interval,
                "samples_stored": span["total"] or 0,
                "observed_since": span["first"],
                "days_observed": span["days"] or 0,
                "read_failures": self.read_failures,
            },
        }


def _maybe_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
