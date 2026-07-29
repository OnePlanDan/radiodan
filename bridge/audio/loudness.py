"""
Per-track loudness measurement and normalisation gain.

The library is not level-consistent: measured 2026-07-29, source loudness across
tracks in rotation spanned -8.9 to -13.6 LUFS, so songs jumped by up to ~5 dB
against each other and against the DJ. Blanket `music_vol` trim fixes the average
but not the spread — only per-track gain does.

Approach is ReplayGain in spirit: measure each file once with ffmpeg's EBU R128
meter, store the integrated loudness, and derive a static per-track gain at queue
time. Static gain rather than a live AGC, because a dynamic normaliser pumps and
reacts *within* a track, which is audible on music.

The gain rides to Liquidsoap on the request URI as
`annotate:replay_gain="<dB>":<path>`, where `amplify(override="replay_gain")`
picks it up. Nothing is re-encoded and no tags are written, so the music files
are never touched.
"""

import asyncio
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# ffmpeg prints the R128 summary to stderr; these pull the integrated figure and
# the true peak out of it.
_INTEGRATED_RE = re.compile(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS")
_TRUE_PEAK_RE = re.compile(r"Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS")

# Silence and near-silence measure as -inf/absurdly low and would demand enormous
# boosts. Anything below this is treated as unmeasurable rather than amplified.
_MIN_SANE_LUFS = -40.0


async def measure_file(
    path: Path, timeout: float = 120.0
) -> tuple[float, float | None] | None:
    """Integrated loudness (LUFS) and true peak (dBFS), or None if unmeasurable.

    Decodes the whole file — that is what makes the figure integrated rather than
    a guess from the first few seconds, and a track's loud chorus vs quiet intro
    is exactly the difference that matters here.

    True peak matters as much as loudness: this library spans -34.7 to +10.0 LUFS,
    so quiet tracks need boosts of 10 dB or more, and without knowing the peak
    there is no way to tell which of those boosts would clip.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            # -nostdin is essential, not cosmetic: under systemd stdin is
            # /dev/null, and ffmpeg reads stdin for interactive commands. It takes
            # the immediate EOF as a quit and exits before decoding anything, which
            # is why measurement worked from a terminal and failed as a service.
            "ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", str(path),
            "-filter:a", "ebur128=peak=true:framelog=quiet", "-f", "null", "-",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError:
        logger.exception("ffmpeg not available for loudness measurement")
        return None

    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning(f"Loudness measurement timed out: {path}")
        return None

    if proc.returncode != 0:
        tail = stderr.decode("utf-8", "replace").strip().splitlines()[-2:]
        logger.warning(
            f"Loudness measurement failed (rc={proc.returncode}): {path.name} — {' | '.join(tail)}"
        )
        return None

    # The summary block repeats the label, so take the last match.
    matches = _INTEGRATED_RE.findall(stderr.decode("utf-8", "replace"))
    if not matches:
        logger.warning(f"No integrated loudness in ffmpeg output: {path.name}")
        return None

    value = float(matches[-1])
    if value < _MIN_SANE_LUFS:
        logger.debug(f"Loudness {value} LUFS below sane floor, treating as unmeasurable: {path}")
        return None

    peaks = _TRUE_PEAK_RE.findall(stderr.decode("utf-8", "replace"))
    true_peak = float(peaks[-1]) if peaks else None
    return value, true_peak


def gain_for(
    loudness_lufs: float | None,
    target_lufs: float,
    assumed_lufs: float,
    max_boost_db: float,
    max_cut_db: float,
    true_peak_dbfs: float | None = None,
    peak_ceiling_dbfs: float = -1.0,
) -> float:
    """Static gain in dB to bring a track to `target_lufs` without clipping it.

    Boost is limited by the track's own headroom, the way ReplayGain's
    prevent-clipping does it, because a blanket boost cap is wrong in both
    directions: too tight and quiet tracks stay quiet, too loose and a peaky one
    clips. There is no per-track limiter downstream to catch a mistake.

    Args:
        loudness_lufs: Measured value, or None if this track hasn't been measured.
        assumed_lufs: Stand-in for unmeasured tracks. Without this, an unmeasured
            track would play at raw level and stick out badly during the backfill,
            which for 7 700 files is not a short window.
        true_peak_dbfs: Measured true peak. When absent, boost falls back to the
            conservative `max_boost_db` cap since headroom is unknown.
        peak_ceiling_dbfs: Highest true peak we are willing to produce.
        max_cut_db: Bound on attenuation. Generous, because cutting cannot clip
            and this library really does contain a track at +10 LUFS that needs
            -26 dB; a tight cut clamp would leave the worst offenders untouched.
    """
    source = loudness_lufs if loudness_lufs is not None else assumed_lufs
    gain = target_lufs - source

    if gain > 0:
        if true_peak_dbfs is not None:
            headroom = peak_ceiling_dbfs - true_peak_dbfs
            gain = min(gain, max(0.0, headroom))
        else:
            gain = min(gain, abs(max_boost_db))
    return max(-abs(max_cut_db), gain)


class LoudnessScanner:
    """Backfills loudness for library tracks that don't have it yet.

    Runs as a background task at low concurrency: measuring 7 700 files means
    7 700 full decodes, and the station has to keep broadcasting while it happens.
    Incremental by nature — once a file is measured it is skipped, so restarts
    resume rather than start over.
    """

    def __init__(
        self,
        db,
        concurrency: int = 3,
        batch_size: int = 200,
        pause_between_batches: float = 5.0,
    ):
        self._db = db
        self.concurrency = max(1, concurrency)
        self.batch_size = max(1, batch_size)
        self.pause_between_batches = pause_between_batches
        self._task: asyncio.Task | None = None
        self.measured = 0
        self.failed = 0

    # A failed file gets loudness_lufs NULL but a loudness_measured_at stamp, so
    # "pending" has to mean "never attempted" — keying only on loudness_lufs made
    # every batch re-fetch the same unmeasurable files forever.
    _PENDING_WHERE = "loudness_lufs IS NULL AND loudness_measured_at IS NULL"

    async def pending_count(self) -> int:
        async with self._db.execute(
            f"SELECT COUNT(*) FROM music_library WHERE {self._PENDING_WHERE}"
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row else 0

    async def _next_batch(self) -> list[str]:
        async with self._db.execute(
            f"SELECT file_path FROM music_library WHERE {self._PENDING_WHERE} LIMIT ?",
            (self.batch_size,),
        ) as cursor:
            return [row[0] async for row in cursor]

    async def _record(
        self, file_path: str, lufs: float | None, true_peak: float | None
    ) -> None:
        # Failures are stamped too, with a NULL reading, so a broken file isn't
        # retried on every pass forever.
        await self._db.execute(
            "UPDATE music_library SET loudness_lufs = ?, true_peak_dbfs = ?, "
            "loudness_measured_at = datetime('now') WHERE file_path = ?",
            (lufs, true_peak, file_path),
        )

    async def run_once(self) -> int:
        """Measure one batch. Returns how many files were processed."""
        paths = await self._next_batch()
        if not paths:
            return 0

        sem = asyncio.Semaphore(self.concurrency)

        async def one(p: str) -> None:
            async with sem:
                result = await measure_file(Path(p))
            lufs, true_peak = result if result is not None else (None, None)
            await self._record(p, lufs, true_peak)
            if lufs is None:
                self.failed += 1
            else:
                self.measured += 1

        await asyncio.gather(*(one(p) for p in paths), return_exceptions=True)
        await self._db.commit()
        return len(paths)

    async def _run(self) -> None:
        remaining = await self.pending_count()
        if remaining:
            logger.info(f"Loudness backfill starting: {remaining} track(s) unmeasured")
        while True:
            try:
                done = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Loudness batch failed")
                await asyncio.sleep(30)
                continue
            if done == 0:
                logger.info(
                    f"Loudness backfill complete: {self.measured} measured, {self.failed} unmeasurable"
                )
                return
            logger.info(
                f"Loudness backfill: {self.measured} measured, {self.failed} unmeasurable, "
                f"{await self.pending_count()} remaining"
            )
            await asyncio.sleep(self.pause_between_batches)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
