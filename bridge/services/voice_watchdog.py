"""
RadioDan Voice Watchdog

Watches for the station going *quietly* mute.

The stuck-stream watchdog in `bridge/audio/stream_context.py` covers dead air:
no track changes, nothing playing. This covers the opposite and much sneakier
failure — music keeps flowing, systemd stays green, Icecast keeps serving, the
API answers, and yet no DJ has spoken in weeks.

That is not hypothetical. Between 2026-06-16 and 2026-07-27 Radio Dan broadcast
41 days of uninterrupted music with zero voice segments: the local TTS host
died, 11 674 generation attempts failed in a row, and every health signal the
system had stayed green because the *stream* was fine. Nothing was watching the
one thing that had actually stopped.

Design notes:
- Escalates once, then reminds on a slow interval. The 2026-05-08 checkup found
  a watchdog that logged every 10 seconds for six days; that is indistinguishable
  from noise and nobody reads it.
- Reports *which* endpoint is unreachable, so the alert is actionable rather
  than just alarming.
- Opens a `voice_outage` row in the event log on breach and closes it on
  recovery, so an outage is queryable after the fact instead of having to be
  reconstructed from log archaeology.
"""

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from bridge.booth import booth

if TYPE_CHECKING:
    from bridge.event_store import EventStore
    from bridge.services.tts_service import TTSService

logger = logging.getLogger(__name__)


def _humanize(seconds: float) -> str:
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, _ = divmod(s, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


class VoiceWatchdog:
    """Alerts when no voice segment has reached air for too long."""

    def __init__(
        self,
        tts_service: "TTSService",
        event_store: "EventStore | None" = None,
        alert_after_seconds: float = 3 * 3600,
        check_interval: float = 300.0,
        reminder_interval: float = 6 * 3600,
    ):
        """
        Args:
            tts_service: Source of truth for when a voice last succeeded.
            event_store: Optional — records outages as `voice_outage` events.
            alert_after_seconds: Silence tolerated before alerting. Should be
                comfortably longer than the producer's rebuild cycle (~50 min)
                so an ordinary slow patch doesn't trip it.
            check_interval: How often to evaluate.
            reminder_interval: Minimum gap between repeat alerts during one
                ongoing outage.
        """
        self.tts_service = tts_service
        self.alert_after_seconds = alert_after_seconds
        self.check_interval = check_interval
        self.reminder_interval = reminder_interval
        self._event_store = event_store

        self._task: asyncio.Task | None = None
        self._alerting = False
        self._last_alert_at = 0.0
        self._outage_event_id: int | None = None
        self._outage_started_at: float | None = None

    def set_event_store(self, event_store: "EventStore") -> None:
        self._event_store = event_store

    # =====================================================================
    # LIFECYCLE
    # =====================================================================

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())
            logger.info(
                "Voice watchdog armed "
                f"(alert after {_humanize(self.alert_after_seconds)} of silence, "
                f"checked every {int(self.check_interval)}s)"
            )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("Voice watchdog stopped")

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.check_interval)
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A broken watchdog must not take the station down with it.
                logger.exception("Voice watchdog check failed")

    # =====================================================================
    # CHECK
    # =====================================================================

    async def check_once(self) -> bool:
        """Evaluate voice health. Returns True if currently in an outage."""
        silent_for = self.tts_service.silent_for_seconds

        if silent_for < self.alert_after_seconds:
            if self._alerting:
                await self._clear(silent_for)
            return False

        now = time.time()
        if not self._alerting:
            await self._raise(silent_for)
        elif now - self._last_alert_at >= self.reminder_interval:
            await self._remind(silent_for)
        return True

    async def _raise(self, silent_for: float) -> None:
        self._alerting = True
        self._last_alert_at = time.time()
        self._outage_started_at = time.time() - silent_for

        detail = await self._diagnose()
        stats = self.tts_service.stats()
        scope = "since startup" if not stats["ever_succeeded"] else "since last success"

        logger.error(
            f"VOICE OUTAGE: no voice segment has reached air for "
            f"{_humanize(silent_for)} ({scope}). {detail}"
        )
        booth.error(f"VOICE OUTAGE — silent {_humanize(silent_for)}. {detail}")

        if self._event_store:
            try:
                self._outage_event_id = await self._event_store.start_event(
                    event_type="voice_outage", lane="system",
                    title=f"Voice outage — silent {_humanize(silent_for)}",
                    details={
                        "silent_for_seconds": f"{silent_for:.0f}",
                        "diagnosis": detail,
                        "last_error": stats["last_error"][:500],
                    },
                )
            except Exception:
                logger.exception("Could not record voice_outage event")

    async def _remind(self, silent_for: float) -> None:
        self._last_alert_at = time.time()
        detail = await self._diagnose()
        logger.error(
            f"VOICE OUTAGE CONTINUES: silent for {_humanize(silent_for)}. {detail}"
        )
        booth.error(f"VOICE OUTAGE continues — {_humanize(silent_for)}. {detail}")

    async def _clear(self, silent_for: float) -> None:
        outage_length = (
            time.time() - self._outage_started_at if self._outage_started_at else None
        )
        self._alerting = False
        self._last_alert_at = 0.0

        recovered = (
            f" after {_humanize(outage_length)}" if outage_length else ""
        )
        logger.info(f"Voice restored{recovered} — voice segments are reaching air again")
        booth.start(f"Voice restored{recovered}")

        if self._event_store and self._outage_event_id is not None:
            try:
                await self._event_store.end_event(
                    self._outage_event_id,
                    extra_details={"outage_seconds": f"{outage_length:.0f}"}
                    if outage_length else None,
                )
            except Exception:
                logger.exception("Could not close voice_outage event")
        self._outage_event_id = None
        self._outage_started_at = None

    async def _diagnose(self) -> str:
        """Name the unreachable endpoints so the alert says what to go fix."""
        try:
            report = await self.tts_service.health_report()
        except Exception:
            return "Endpoint probe failed."

        down = [ep for ep, ok in report.items() if not ok]
        up = [ep for ep, ok in report.items() if ok]
        if not down:
            last_error = self.tts_service.stats()["last_error"]
            return (
                "All TTS endpoints are reachable, so this is not a dead host — "
                f"check generation errors: {last_error[:200] or 'none recorded'}"
            )
        parts = [f"Unreachable: {', '.join(down)}."]
        if up:
            parts.append(f"Reachable: {', '.join(up)} — consider a fallback route.")
        else:
            parts.append("No TTS endpoint is reachable.")
        return " ".join(parts)

    # =====================================================================
    # STATUS
    # =====================================================================

    def status(self) -> dict:
        """Watchdog state for /api/status and /api/status/health."""
        silent_for = self.tts_service.silent_for_seconds
        return {
            "alerting": self._alerting,
            "silent_for_seconds": round(silent_for, 1),
            "silent_for": _humanize(silent_for),
            "alert_after_seconds": self.alert_after_seconds,
            "outage_started_at": self._outage_started_at,
            **self.tts_service.stats(),
        }
