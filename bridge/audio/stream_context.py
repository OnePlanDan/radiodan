"""
RadioDan Stream Context

Real-time "what's playing" monitor. Polls Liquidsoap for track metadata
and timing, emitting events when tracks change or approach their end.

Events:
- "track_changed" — fired when the playing filename changes
- "track_ending" — fired when remaining seconds drops below threshold
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from bridge.audio.mixer import LiquidsoapMixer
from bridge.booth import booth

if TYPE_CHECKING:
    from bridge.audio.playlist_planner import PlaylistPlanner
    from bridge.event_store import EventStore

logger = logging.getLogger(__name__)

# Type alias for async event callbacks
EventCallback = Callable[..., Coroutine[Any, Any, None]]


class StreamContext:
    """
    Monitors Liquidsoap stream state and emits events.

    Polls the mixer every `poll_interval` seconds for metadata and timing.
    Enrichments are a shared dict that plugins can write to and read from;
    they are cleared on each track change. Feeder context persists across tracks.
    """

    def __init__(
        self,
        mixer: LiquidsoapMixer,
        poll_interval: float = 2.0,
        track_ending_threshold: float = 30.0,
        grace_seconds: float = 10.0,
        min_track_duration: float = 10.0,
        liquidsoap_container_name: str = "radiodan-agent-liquidsoap-1",
        fallback_track_path: Path | None = None,
    ):
        self.mixer = mixer
        self.poll_interval = poll_interval
        self.track_ending_threshold = track_ending_threshold

        # Current state
        self.current_track: dict = {}
        self.remaining_seconds: float = 0.0
        self.elapsed_seconds: float = 0.0
        self.enrichments: dict[str, Any] = {}

        # Feeder context: data from ContextFeeder plugins, NOT cleared on track change
        self.feeder_context: dict[str, Any] = {}

        # Playlist planner reference (set after construction)
        self._planner: "PlaylistPlanner | None" = None

        # Event store for timeline (optional)
        self._event_store: "EventStore | None" = None

        # Event subscribers: event_name -> list of async callbacks
        self._listeners: dict[str, list[EventCallback]] = {}

        # Internal state for change detection
        self._last_filename: str = ""
        self._track_ending_fired: bool = False

        # Background poller task
        self._poll_task: asyncio.Task | None = None

        # Idle-poll watchdog: consecutive polls with nothing playing (legacy, kept as belt-and-suspenders)
        self._idle_polls: int = 0
        self._IDLE_RECOVERY_THRESHOLD: int = 5  # recover after ~10s of silence

        # Track-bounded watchdog: deadline = (track start) + (duration) + (grace).
        # If the deadline passes without a track_changed event, escalate via _escalate_stuck.
        self._grace_seconds = grace_seconds
        self._min_track_duration = min_track_duration
        self._container_name = liquidsoap_container_name
        self._fallback_track_path = fallback_track_path
        self._track_deadline_monotonic: float | None = None
        self._stuck_strikes: int = 0

    def set_planner(self, planner: "PlaylistPlanner") -> None:
        """Set the playlist planner reference for upcoming track info."""
        self._planner = planner

    def set_event_store(self, event_store: "EventStore") -> None:
        """Set the event store for timeline instrumentation."""
        self._event_store = event_store

    @property
    def upcoming_tracks(self) -> list[dict]:
        """Upcoming tracks from the playlist planner."""
        if self._planner:
            return self._planner.upcoming
        return []

    @property
    def next_track_info(self) -> dict | None:
        """Info about the next track to play, if known."""
        upcoming = self.upcoming_tracks
        return upcoming[0] if upcoming else None

    def on(self, event: str, callback: EventCallback) -> None:
        """Subscribe to a stream event.

        Args:
            event: Event name ("track_changed" or "track_ending")
            callback: Async function to call when event fires
        """
        self._listeners.setdefault(event, []).append(callback)

    def off(self, event: str, callback: EventCallback) -> None:
        """Unsubscribe from a stream event."""
        if event in self._listeners:
            try:
                self._listeners[event].remove(callback)
            except ValueError:
                pass

    async def _emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        """Emit an event to all subscribers. Errors are caught and logged."""
        for callback in self._listeners.get(event, []):
            try:
                await callback(*args, **kwargs)
            except Exception:
                logger.exception(f"Error in {event} listener {callback.__qualname__}")

    async def _poll(self) -> None:
        """Background loop: poll Liquidsoap and detect state changes."""
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Stream context poll error")
            await asyncio.sleep(self.poll_interval)

    async def _push_metadata(self, track_info: dict) -> None:
        """Push enriched metadata to Liquidsoap so Icecast updates ICY for stream clients."""
        parts = []
        for key in ("artist", "title", "album", "genre", "year"):
            value = (track_info.get(key) or "").replace(",", " ")  # comma is separator
            parts.append(f"{key}={value}")
        cmd = ",".join(parts)
        try:
            await self.mixer._send_command(f"music.set_metadata {cmd}")
        except Exception:
            logger.debug("Failed to push metadata to Liquidsoap")

    def _enrich_from_planner(self, track_info: dict) -> dict:
        """Override Liquidsoap metadata with planner's ID3-sourced metadata.

        Liquidsoap's last_metadata() can return stale fields during crossfade.
        The planner has correct metadata from mutagen, so prefer it when available.
        """
        if not self._planner:
            return track_info

        filename = track_info.get("filename", "")
        if not filename:
            return track_info

        target = Path(filename).name
        match = None

        # Search upcoming queue first (playing track is still at [0])
        for track in self._planner.upcoming:
            if Path(track.get("file_path", "")).name == target:
                match = track
                break

        # Fallback: search full library
        if not match:
            for track in self._planner.library:
                if Path(track.get("file_path", "")).name == target:
                    match = track
                    break

        if not match:
            return track_info

        enriched = dict(track_info)
        for key in ("artist", "title", "album", "genre", "year"):
            value = match.get(key, "")
            if value:
                enriched[key] = value
        if match.get("duration_seconds"):
            enriched["duration_seconds"] = match["duration_seconds"]
        return enriched

    async def _poll_once(self) -> None:
        """Single poll iteration: query state and emit events."""
        # Query all three in sequence (they share the telnet lock)
        track_info = await self.mixer.get_track_info()
        remaining = await self.mixer.get_remaining()
        elapsed = await self.mixer.get_elapsed()

        self.current_track = track_info
        self.remaining_seconds = remaining
        self.elapsed_seconds = elapsed

        current_filename = track_info.get("filename", "")

        # Detect track change
        if current_filename and current_filename != self._last_filename:
            self._last_filename = current_filename
            self._track_ending_fired = False
            self.enrichments.clear()

            # Enrich with planner metadata (Liquidsoap metadata lags during crossfades)
            track_info = self._enrich_from_planner(track_info)
            self.current_track = track_info

            # Push correct metadata to Liquidsoap → Icecast (fixes stale ICY metadata)
            await self._push_metadata(track_info)

            artist = track_info.get("artist", "Unknown")
            title = track_info.get("title", "Unknown")
            booth.track_change(artist, title)
            logger.info(f"Track changed: {artist} - {title}")

            await self._emit("track_changed", track_info)

            # Track-bounded watchdog: arm a deadline based on track duration + grace.
            # If the next track_changed doesn't fire by then, the stream is stuck.
            duration = float(track_info.get("duration_seconds") or 0)
            if duration >= self._min_track_duration:
                self._track_deadline_monotonic = (
                    time.monotonic() + duration + self._grace_seconds
                )
            else:
                # Suspiciously short or missing duration — don't enforce.
                self._track_deadline_monotonic = None
            self._stuck_strikes = 0

        # Detect track ending
        if (
            remaining > 0
            and remaining < self.track_ending_threshold
            and not self._track_ending_fired
        ):
            self._track_ending_fired = True
            logger.info(f"Track ending in {remaining:.1f}s")
            await self._emit("track_ending", remaining)

        # Idle-poll watchdog (legacy): recover from empty Liquidsoap queue.
        # Kept as belt-and-suspenders next to the track-bounded watchdog below.
        if not current_filename and remaining == 0:
            self._idle_polls += 1
            if (
                self._idle_polls >= self._IDLE_RECOVERY_THRESHOLD
                and self._idle_polls % self._IDLE_RECOVERY_THRESHOLD == 0
                and self._planner
            ):
                await self._try_recover_queue()
        else:
            self._idle_polls = 0

        # Track-bounded watchdog: trip if we've passed the expected end + grace
        # without seeing a track_changed event.
        if (
            self._track_deadline_monotonic is not None
            and time.monotonic() > self._track_deadline_monotonic
        ):
            await self._escalate_stuck()

    async def _escalate_stuck(self) -> None:
        """Track-bounded watchdog escalation ladder.

        Called when a track plays past `expected_end + grace` without the
        next track_changed event firing. Each call advances one strike and
        re-arms a short deadline so the next strike fires if this one didn't help.
        Strikes reset on the next successful track_changed.

            Strike 1: music_q.skip — nudge LS past a single bad request.
            Strike 2: flush + push known-good fallback (if configured).
            Strike 3: flush + re-push the planner's full upcoming batch.
            Strike 4+: docker restart of the Liquidsoap container.
        """
        self._stuck_strikes += 1
        strike = self._stuck_strikes

        # Re-arm deadline so we'll escalate again if this strike didn't help.
        # 30s gives Liquidsoap time to start the next track after the action.
        self._track_deadline_monotonic = time.monotonic() + 30.0

        logger.warning(f"Stuck-stream watchdog strike {strike} firing")
        booth.start(f"Stuck-stream watchdog strike {strike}")

        try:
            if strike == 1:
                await self.mixer.next_track()
            elif strike == 2:
                await self.mixer.flush_queued_music()
                if self._fallback_track_path is not None:
                    ok = await self.mixer.queue_music(self._fallback_track_path)
                    if not ok:
                        logger.error(
                            f"Fallback track failed to queue: {self._fallback_track_path}"
                        )
                elif self._planner:
                    # No fallback configured — fall through to a full re-push
                    await self._planner._push_all_to_liquidsoap()
            elif strike == 3:
                await self.mixer.flush_queued_music()
                if self._planner:
                    await self._planner._push_all_to_liquidsoap()
            else:
                # Strike 4+ — restart the container, then reset state for a clean slate.
                logger.error(
                    f"Restarting Liquidsoap container '{self._container_name}' "
                    f"(strike {strike}, last-resort recovery)"
                )
                booth.start(f"Restart LS container (strike {strike})")
                await self.mixer.restart_liquidsoap_container(self._container_name)
                self._stuck_strikes = 0
                self._track_deadline_monotonic = None
                self._last_filename = ""
        except Exception:
            logger.exception(f"Watchdog strike {strike} action raised")

    async def _try_recover_queue(self) -> None:
        """Re-push tracks when Liquidsoap's queue has drained.

        Called by the watchdog after several consecutive idle polls.
        Resets the last-filename tracker so the next track that plays
        will properly fire a track_changed event.
        """
        ls_count = await self.mixer.get_music_queue_length()
        if ls_count > 0:
            return  # Liquidsoap has tracks, just waiting for playback

        planner_count = len(self._planner.upcoming)
        if planner_count == 0:
            return  # Nothing to push

        logger.warning(
            f"Watchdog: Liquidsoap queue empty but planner has {planner_count} "
            f"tracks — re-pushing"
        )
        booth.start(f"Queue recovery ({planner_count} tracks re-pushed)")
        await self._planner._push_all_to_liquidsoap()
        # Reset filename tracker so the next playing track triggers track_changed
        self._last_filename = ""

    async def notify_skip(self, source: str = "listener") -> None:
        """Force an immediate poll after a skip, so events transition instantly.

        `source` says who skipped. Only a listener's skip is emitted as a
        "skip" event — that event exists so the DJ can react to a human
        rejecting a song. A system skip (the greeter breaking a song for the
        bulletin) must not read as taste: reacting to it made the producer
        insert its own track at position 0, racing the very episode the skip
        was for (seen live 2026-08-14 09:40:30 — France Gall beat June Ferry).
        """
        if source == "listener":
            await self._emit("skip", self.current_track or {})
        if self._planner:
            self._planner.notify_skip()
        await self._poll_once()

    async def start(self) -> None:
        """Start the background poller."""
        if self._poll_task is not None:
            return
        # Recover state from previous run to prevent re-firing on restart
        if self._event_store:
            last_fn = await self._event_store.get_last_music_filename()
            if last_fn:
                self._last_filename = last_fn
                logger.info(f"Recovered last filename: {Path(last_fn).name}")
        self._poll_task = asyncio.create_task(self._poll())
        booth.start(f"Stream context (polling every {self.poll_interval}s)")
        logger.info(f"Stream context started (polling every {self.poll_interval}s)")

    async def stop(self) -> None:
        """Stop the background poller."""
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
            booth.stop("Stream context")
            logger.info("Stream context stopped")
