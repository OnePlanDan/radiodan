"""
Commissioning — ordering programme episodes and getting them on air.

The station commissions an episode from AudioSegment against a brief, waits
~20 minutes, receives mastered audio, and schedules it like a song. That last
part is the whole design: an episode is a queue item with a file path, not a
separate playout path. Same queue, same crossfade, same normalisation.

Why this is a background service rather than a request/response call: the
producer's own history gives `build ≈ 13.0 + 1.04 × length` minutes, so a
7-minute episode takes ~20 minutes and the p90 for that size is 34. Nothing
here can be ordered at air time.

Two rules hold the station's priority — keep streaming, whatever happens:

- A commission is *speculative*. Music is always the fallback; a late or failed
  episode costs nothing but its build.
- Nothing airs until it is on local disk and measured. A file that fails to
  download or fails its loudness check is never queued.
"""

import asyncio
import logging
import re
import time
from pathlib import Path

from bridge.audio.loudness import measure_file
from bridge.services.audiosegment import (
    DONE,
    FAILED,
    AudioSegmentClient,
    AudioSegmentError,
    is_terminal,
)

logger = logging.getLogger(__name__)

COMMISSION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS commissions (
    job_id           TEXT PRIMARY KEY,
    show             TEXT NOT NULL,
    concept          TEXT NOT NULL,
    state            TEXT NOT NULL,
    remote_status    TEXT,
    requested_at     REAL NOT NULL,
    delivered_at     REAL,
    aired_at         REAL,
    file_path        TEXT,
    title            TEXT,
    duration_seconds REAL,
    loudness_lufs    REAL,
    true_peak_dbfs   REAL,
    error            TEXT
);
CREATE INDEX IF NOT EXISTS idx_commissions_state ON commissions(state);
"""

# Station-side lifecycle, distinct from AudioSegment's job status.
PENDING = "pending"    # ordered, still being produced
READY = "ready"        # on local disk, measured, schedulable
AIRED = "aired"        # has been queued for broadcast
FAILED_STATE = "failed"

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_-]+")


class CommissionService:
    """Orders episodes, collects them when they land, offers them for scheduling."""

    def __init__(
        self,
        client: AudioSegmentClient,
        db_path,
        programme_dir: Path,
        poll_interval: float = 60.0,
        auto_requeue: bool = True,
        owned_shows: list[str] | None = None,
    ):
        self.client = client
        self.db_path = db_path
        self.programme_dir = Path(programme_dir)
        self.poll_interval = max(15.0, poll_interval)
        self.auto_requeue = auto_requeue
        # Commissioning writes to a live series — episode number, recap, traits,
        # debts. Only shows this station owns are ours to order against.
        self.owned_shows = set(owned_shows or [])
        self._db = None
        self._task: asyncio.Task | None = None
        self._requeued: set[str] = set()

    # =====================================================================
    # LIFECYCLE
    # =====================================================================

    async def start(self) -> None:
        import aiosqlite

        self.programme_dir.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA busy_timeout = 5000")
        await self._db.executescript(COMMISSION_SCHEMA_SQL)
        await self._db.commit()
        self._task = asyncio.create_task(self._run())
        outstanding = len(await self.pending())
        logger.info(
            f"Commissioning started (polling every {int(self.poll_interval)}s, "
            f"{outstanding} outstanding, episodes in {self.programme_dir})"
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._db:
            await self._db.close()
            self._db = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A stuck collector must never take the station down.
                logger.exception("Commission poll failed")

    # =====================================================================
    # ORDERING
    # =====================================================================

    async def commission(
        self,
        show: str,
        concept: str,
        location: str | None = None,
        weight: int | None = None,
        context_mode: str | None = None,
    ) -> dict:
        """Order an episode. Returns the commission row; audio arrives later."""
        if show not in self.owned_shows:
            raise PermissionError(
                f"'{show}' is not a show this station owns "
                f"(owned: {sorted(self.owned_shows) or 'none yet'}). "
                "Commissioning advances a series' episode number, recap and trait "
                "state, so it is only done against shows we created."
            )
        result = await self.client.produce(
            show, concept, location=location, weight=weight, context_mode=context_mode
        )
        job_id = result["job_id"]
        await self._db.execute(
            "INSERT OR REPLACE INTO commissions "
            "(job_id, show, concept, state, remote_status, requested_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, show, concept, PENDING, result.get("status"), time.time()),
        )
        await self._db.commit()
        return await self.get(job_id)

    # =====================================================================
    # COLLECTING
    # =====================================================================

    async def poll_once(self) -> int:
        """Check every outstanding commission. Returns how many were collected."""
        collected = 0
        for row in await self.pending():
            try:
                if await self._check(row):
                    collected += 1
            except AudioSegmentError as e:
                # Service down or flaky: leave it pending and try again next tick.
                logger.warning(f"Could not check commission {row['job_id']}: {e}")
            except Exception:
                logger.exception(f"Collecting commission {row['job_id']} failed")
        return collected

    async def _check(self, row) -> bool:
        job_id = row["job_id"]
        job = await self.client.job(job_id)
        status = job.get("status")

        if status != row["remote_status"]:
            await self._db.execute(
                "UPDATE commissions SET remote_status = ? WHERE job_id = ?", (status, job_id)
            )
            await self._db.commit()

        if not is_terminal(status):
            return False

        if status == FAILED:
            await self._handle_failure(job_id, job)
            return False

        if status == DONE:
            return await self._collect(job_id, row["show"], job)
        return False

    async def _handle_failure(self, job_id: str, job: dict) -> None:
        error = job.get("error") or "unknown error"
        # One automatic retry: a requeue resumes at `scripted` when a script
        # exists, so it does not repeat the expensive scriptwriting.
        if self.auto_requeue and job_id not in self._requeued:
            self._requeued.add(job_id)
            logger.warning(f"Commission {job_id} failed ({error}) — requeuing once")
            try:
                await self.client.requeue(job_id)
                await self._db.execute(
                    "UPDATE commissions SET remote_status = ?, error = ? WHERE job_id = ?",
                    ("queued", f"retried after: {error}", job_id),
                )
                await self._db.commit()
                return
            except AudioSegmentError as e:
                logger.warning(f"Requeue of {job_id} rejected: {e}")

        logger.error(f"Commission {job_id} failed for good: {error}")
        await self._db.execute(
            "UPDATE commissions SET state = ?, error = ? WHERE job_id = ?",
            (FAILED_STATE, error, job_id),
        )
        await self._db.commit()

    async def _collect(self, job_id: str, show: str, job: dict) -> bool:
        """Download, measure, and mark ready. Nothing airs before all three."""
        dest = self.programme_dir / f"{_SAFE_NAME.sub('-', show)}-{job_id[:8]}.mp3"
        await self.client.download_audio(job_id, dest)

        # Measure on receipt: this is the delivery spec check. Storing loudness
        # and peak means the episode gets the same per-track gain as a song, so
        # it airs at the station's level without a separate path.
        measured = await measure_file(dest)
        if measured is None:
            logger.error(f"Episode {job_id} could not be measured — not scheduling it")
            await self._db.execute(
                "UPDATE commissions SET state = ?, error = ?, file_path = ? WHERE job_id = ?",
                (FAILED_STATE, "delivered audio could not be measured", str(dest), job_id),
            )
            await self._db.commit()
            return False

        lufs, peak = measured
        duration = await _duration_of(dest)
        await self._db.execute(
            "UPDATE commissions SET state = ?, remote_status = ?, delivered_at = ?, "
            "file_path = ?, title = ?, duration_seconds = ?, loudness_lufs = ?, "
            "true_peak_dbfs = ?, error = NULL WHERE job_id = ?",
            (READY, DONE, time.time(), str(dest), job.get("script_title"),
             duration, lufs, peak, job_id),
        )
        await self._db.commit()
        logger.info(
            f"Episode ready: {show} {job_id[:8]} — "
            f"{(duration or 0) / 60:.1f} min, {lufs} LUFS, peak {peak} dBFS"
        )
        return True

    # =====================================================================
    # SCHEDULING
    # =====================================================================

    def to_queue_item(self, row) -> dict:
        """A commission as a queue item — the same shape a song has."""
        return {
            "file_path": row["file_path"],
            "artist": row["show"],
            "title": row["title"] or row["concept"][:60],
            "album": "",
            "genre": "programme",
            "year": "",
            "duration_seconds": row["duration_seconds"],
            "loudness_lufs": row["loudness_lufs"],
            "true_peak_dbfs": row["true_peak_dbfs"],
            "programme": True,
            "job_id": row["job_id"],
        }

    async def mark_aired(self, job_id: str) -> None:
        await self._db.execute(
            "UPDATE commissions SET state = ?, aired_at = ? WHERE job_id = ?",
            (AIRED, time.time(), job_id),
        )
        await self._db.commit()

    # =====================================================================
    # QUERIES
    # =====================================================================

    async def _rows(self, where: str, params: tuple = ()) -> list:
        async with self._db.execute(
            f"SELECT * FROM commissions {where}", params
        ) as cursor:
            return [row async for row in cursor]

    async def pending(self) -> list:
        return await self._rows("WHERE state = ? ORDER BY requested_at", (PENDING,))

    async def ready(self) -> list:
        """Delivered, measured, not yet queued — oldest first."""
        return await self._rows("WHERE state = ? ORDER BY delivered_at", (READY,))

    async def get(self, job_id: str):
        rows = await self._rows("WHERE job_id = ?", (job_id,))
        return rows[0] if rows else None

    async def recent(self, limit: int = 20) -> list:
        return await self._rows("ORDER BY requested_at DESC LIMIT ?", (limit,))


async def _duration_of(path: Path) -> float | None:
    """Duration in seconds via ffprobe, or None."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        return float(stdout.decode().strip())
    except Exception:
        return None
