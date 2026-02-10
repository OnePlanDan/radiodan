"""
RadioDan Voice Scheduler

Central voice timing engine. Plugins submit VoiceSegments with trigger
modes; the scheduler determines when to generate TTS and play them.

Trigger modes:
- "asap"             -> immediately generate and queue
- "between_songs"    -> play when current track ends (priority-ordered)
- "before_end:X"     -> play X seconds before current track ends
- "after_start:X"    -> play X seconds after current track started
- "bridge"           -> straddle the crossfade between tracks (precise timing)

Bridge mix modes:
- "duck"         -> route through tts.push (standard ducking, music at duck_amount)
- "gentle_duck"  -> temporarily raise duck_amount to 0.25, restore after
- "overlay"      -> route through earcons.push (no ducking at all)
"""

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from bridge.audio.mixer import LiquidsoapMixer
from bridge.audio.stream_context import StreamContext
from bridge.booth import booth

logger = logging.getLogger(__name__)

# Forward reference resolved at runtime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bridge.services.tts_service import TTSService
    from bridge.event_store import EventStore


@dataclass
class VoiceSegment:
    """A segment of voice to be played on the stream."""

    text: str
    trigger: str = "asap"  # "asap", "between_songs", "before_end:20", "after_start:30", "bridge"
    priority: int = 0  # Lower = plays first in between-songs queue
    hold_next_song: bool = False
    leading_silence: float = 0.0
    trailing_silence: float = 0.0
    source_plugin: str = ""

    # Pre-generated audio support (skip TTS generation if set)
    pre_generated_audio: Path | None = None
    audio_duration: float = 0.0  # Duration in seconds (for bridge timing math)

    # Bridge mix mode: how to mix voice over music during bridges
    bridge_mix: str = "duck"  # "duck" | "gentle_duck" | "overlay"

    # Per-segment voice overrides (None = use TTS service defaults)
    speaker: str | None = None
    instruct: str | None = None

    # Event store tracking ID (set by VoiceScheduler instrumentation)
    _event_id: int | None = None


class VoiceScheduler:
    """
    Central voice timing engine.

    Subscribes to stream context events and plays voice segments
    at the right moment relative to the music stream.
    """

    def __init__(
        self,
        tts_service: "TTSService",
        mixer: LiquidsoapMixer,
        stream_context: StreamContext,
    ):
        self.tts_service = tts_service
        self.mixer = mixer
        self.stream_context = stream_context

        # Queue for between-songs segments (sorted by priority on flush)
        self._between_queue: list[VoiceSegment] = []

        # Timed triggers: list of (threshold_seconds, segment) for before_end / after_start
        self._before_end_triggers: list[tuple[float, VoiceSegment]] = []
        self._after_start_triggers: list[tuple[float, VoiceSegment]] = []

        # Track which timed triggers have fired this track (separate sets to avoid index collision)
        self._fired_before_end: set[int] = set()
        self._fired_after_start: set[int] = set()

        # Lock for queue operations
        self._lock = asyncio.Lock()

        # Event store for timeline (optional)
        self._event_store: "EventStore | None" = None

    def set_event_store(self, event_store: "EventStore") -> None:
        """Set the event store for timeline instrumentation."""
        self._event_store = event_store

    async def submit(self, segment: VoiceSegment) -> None:
        """
        Submit a voice segment for scheduled playback.

        Priority < 0 with trigger "asap" = urgent interrupt: flush current
        voice playback and cancel lower-priority queued segments.

        Args:
            segment: The voice segment with trigger timing
        """
        trigger = segment.trigger
        source = segment.source_plugin or "unknown"
        preview = segment.text[:40] + "..." if len(segment.text) > 40 else segment.text

        # Priority interruption: urgent segments flush the voice queue
        if trigger == "asap" and segment.priority < 0:
            logger.info(f"[{source}] Voice INTERRUPT (pri={segment.priority}): {preview}")
            booth.plugin_event(source, f"Interrupt: {preview}")
            if self._event_store:
                segment._event_id = await self._event_store.start_event(
                    event_type="voice_segment", lane=source,
                    title=preview, status="active",
                    details={"trigger": "interrupt", "priority": segment.priority, "text": segment.text, "duration_seconds": segment.audio_duration},
                )
            await self._interrupt_for(segment)
            return

        if trigger == "asap":
            logger.info(f"[{source}] Voice ASAP: {preview}")
            booth.plugin_event(source, f"Voice: {preview}")
            if self._event_store:
                segment._event_id = await self._event_store.start_event(
                    event_type="voice_segment", lane=source,
                    title=preview, status="active",
                    details={"trigger": trigger, "priority": segment.priority, "text": segment.text, "duration_seconds": segment.audio_duration},
                )
            await self._play(segment)

        elif trigger == "between_songs":
            if self._event_store:
                segment._event_id = await self._event_store.start_event(
                    event_type="voice_segment", lane=source,
                    title=preview, status="scheduled",
                    details={"trigger": trigger, "priority": segment.priority, "text": segment.text, "duration_seconds": segment.audio_duration},
                )
            async with self._lock:
                self._between_queue.append(segment)
            logger.info(f"[{source}] Voice queued (between songs, pri={segment.priority}): {preview}")
            booth.plugin_event(source, f"Queued between songs: {preview}")

        elif trigger == "bridge":
            if self._event_store:
                segment._event_id = await self._event_store.start_event(
                    event_type="voice_segment", lane=source,
                    title=preview, status="scheduled",
                    details={"trigger": trigger, "priority": segment.priority, "text": segment.text, "duration_seconds": segment.audio_duration},
                )
            await self._schedule_bridge(segment)

        elif trigger.startswith("before_end:"):
            try:
                seconds = float(trigger.split(":")[1])
                if self._event_store:
                    segment._event_id = await self._event_store.start_event(
                        event_type="voice_segment", lane=source,
                        title=preview, status="scheduled",
                        details={"trigger": trigger, "priority": segment.priority, "text": segment.text, "duration_seconds": segment.audio_duration},
                    )
                async with self._lock:
                    self._before_end_triggers.append((seconds, segment))
                logger.info(f"[{source}] Voice timed (before_end:{seconds}s): {preview}")
            except (IndexError, ValueError):
                logger.error(f"Invalid trigger format: {trigger}")

        elif trigger.startswith("after_start:"):
            try:
                seconds = float(trigger.split(":")[1])
                if self._event_store:
                    segment._event_id = await self._event_store.start_event(
                        event_type="voice_segment", lane=source,
                        title=preview, status="scheduled",
                        details={"trigger": trigger, "priority": segment.priority, "text": segment.text, "duration_seconds": segment.audio_duration},
                    )
                async with self._lock:
                    self._after_start_triggers.append((seconds, segment))
                logger.info(f"[{source}] Voice timed (after_start:{seconds}s): {preview}")
            except (IndexError, ValueError):
                logger.error(f"Invalid trigger format: {trigger}")

        else:
            logger.warning(f"Unknown trigger mode: {trigger}")

    async def _schedule_bridge(self, segment: VoiceSegment) -> None:
        """Schedule a bridge voice to straddle the crossfade equally.

        Bridge timing math:
          Voice midpoint should align with crossfade midpoint.
          trigger_at = (voice_duration + crossfade_duration) / 2

          Example: voice=8s, crossfade=5s -> trigger at 6.5s before song end
            Voice:     starts at 6.5s remaining, ends 1.5s into next song
            Crossfade: starts at 5.0s remaining, ends at 0s
            Both midpoints: 2.5s before song end
        """
        crossfade_dur = await self.mixer.get_crossfade_duration()
        voice_dur = segment.audio_duration

        if voice_dur <= 0:
            # No duration info — fall back to before_end with crossfade duration
            logger.warning("Bridge segment has no audio_duration, falling back to before_end")
            trigger_at = crossfade_dur
        else:
            trigger_at = (voice_dur + crossfade_dur) / 2

        source = segment.source_plugin or "unknown"
        logger.info(
            f"[{source}] Bridge scheduled: voice={voice_dur:.1f}s, "
            f"crossfade={crossfade_dur:.1f}s, trigger_at={trigger_at:.1f}s before end"
        )
        booth.plugin_event(source, f"Bridge timed: {trigger_at:.1f}s before end")

        async with self._lock:
            self._before_end_triggers.append((trigger_at, segment))

    async def _interrupt_for(self, segment: VoiceSegment) -> None:
        """High-priority segment interrupts current voice playback.

        Flushes the Liquidsoap TTS queue and cancels lower-priority
        between-songs segments, then plays the interrupt segment.
        """
        # Flush TTS queue in Liquidsoap
        await self.mixer.flush_tts()

        # Cancel lower-priority between-songs segments
        async with self._lock:
            kept = [s for s in self._between_queue if s.priority <= segment.priority]
            cancelled = [s for s in self._between_queue if s.priority > segment.priority]
            self._between_queue = kept

        for s in cancelled:
            if self._event_store and s._event_id is not None:
                await self._event_store.end_event(s._event_id, status="cancelled")

        logger.info(f"Interrupt: flushed TTS queue, cancelled {len(cancelled)} queued segments")

        # Play the interrupt segment immediately
        await self._play(segment)

    async def _play(self, segment: VoiceSegment) -> None:
        """Generate TTS (or use pre-generated audio) and queue for playback."""
        try:
            # Mark event as active
            if self._event_store and segment._event_id is not None:
                await self._event_store.update_event(segment._event_id, status="active")

            # Use pre-generated audio if available, otherwise generate via TTS
            if segment.pre_generated_audio and segment.pre_generated_audio.exists():
                audio_path = segment.pre_generated_audio
                logger.info(f"Using pre-generated audio: {audio_path.name}")
            else:
                audio_path = await self.tts_service.speak(
                    segment.text,
                    speaker=segment.speaker,
                    instruct=segment.instruct,
                )

            if segment.leading_silence > 0:
                await asyncio.sleep(segment.leading_silence)

            # Route based on bridge mix mode
            await self._queue_with_mix_mode(audio_path, segment.bridge_mix)

            if segment.trailing_silence > 0:
                await asyncio.sleep(segment.trailing_silence)

            # Mark event as completed
            if self._event_store and segment._event_id is not None:
                await self._event_store.end_event(segment._event_id)

        except Exception:
            logger.exception(f"Failed to play voice segment from {segment.source_plugin}")
            booth.plugin_error(
                segment.source_plugin or "scheduler",
                f"Voice playback failed: {segment.text[:30]}",
            )
            if self._event_store and segment._event_id is not None:
                await self._event_store.end_event(segment._event_id, status="failed")

    async def _queue_with_mix_mode(self, audio_path: Path, mix_mode: str) -> None:
        """Queue audio with the specified mix mode.

        Mix modes:
          duck:        standard TTS ducking (music at duck_amount)
          gentle_duck: temporarily raise duck_amount to 0.25, restore after
          overlay:     route through earcons queue (no ducking)
        """
        if mix_mode == "overlay":
            await self.mixer.queue_earcon(audio_path)
        elif mix_mode == "gentle_duck":
            # Read current duck amount, set to gentler level, queue, then restore
            # persist=False: don't save temporary duck override to database
            volumes = await self.mixer.get_volumes()
            original_duck = volumes.get("duck_amount", 0.15)
            await self.mixer.set_duck_amount(0.25, persist=False)
            await self.mixer.queue_tts(audio_path)
            # Schedule restore after a delay (approximate voice duration)
            asyncio.get_event_loop().call_later(
                10.0,  # Conservative delay to allow voice to finish
                lambda: asyncio.ensure_future(self.mixer.set_duck_amount(original_duck, persist=False)),
            )
        else:
            # Default "duck" mode
            await self.mixer.queue_tts(audio_path)

    async def _on_track_changed(self, track_info: dict) -> None:
        """Handle track change: flush between-songs queue in priority order."""
        async with self._lock:
            # Clear timed triggers from previous track
            self._before_end_triggers.clear()
            self._after_start_triggers.clear()
            self._fired_before_end.clear()
            self._fired_after_start.clear()

            # Flush between-songs queue
            if not self._between_queue:
                return

            # Sort by priority (lower = first)
            queue = sorted(self._between_queue, key=lambda s: s.priority)
            self._between_queue.clear()

        logger.info(f"Playing {len(queue)} queued voice segments between songs")

        for segment in queue:
            await self._play(segment)

    async def _on_track_ending(self, remaining: float) -> None:
        """Handle track ending: check before_end triggers."""
        async with self._lock:
            triggers_to_fire = []
            for i, (threshold, segment) in enumerate(self._before_end_triggers):
                if remaining <= threshold and i not in self._fired_before_end:
                    self._fired_before_end.add(i)
                    triggers_to_fire.append(segment)

        for segment in triggers_to_fire:
            await self._play(segment)

    async def _check_after_start(self) -> None:
        """Periodic check for after_start triggers based on elapsed time."""
        elapsed = self.stream_context.elapsed_seconds
        if elapsed <= 0:
            return

        async with self._lock:
            triggers_to_fire = []
            for i, (threshold, segment) in enumerate(self._after_start_triggers):
                if elapsed >= threshold and i not in self._fired_after_start:
                    self._fired_after_start.add(i)
                    triggers_to_fire.append(segment)

        for segment in triggers_to_fire:
            await self._play(segment)

    async def _monitor_loop(self) -> None:
        """Background loop checking timed triggers."""
        while True:
            try:
                await self._check_after_start()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Voice scheduler monitor error")
            await asyncio.sleep(2.0)

    async def start(self) -> None:
        """Start the voice scheduler and subscribe to stream events."""
        self.stream_context.on("track_changed", self._on_track_changed)
        self.stream_context.on("track_ending", self._on_track_ending)
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        booth.start("Voice scheduler")
        logger.info("Voice scheduler started")

    async def stop(self) -> None:
        """Stop the voice scheduler."""
        if hasattr(self, "_monitor_task") and self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        booth.stop("Voice scheduler")
        logger.info("Voice scheduler stopped")
