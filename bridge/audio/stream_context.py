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
        self._current_track_event_id: int | None = None

        # Event subscribers: event_name -> list of async callbacks
        self._listeners: dict[str, list[EventCallback]] = {}

        # Internal state for change detection
        self._last_filename: str = ""
        self._track_ending_fired: bool = False

        # Background poller task
        self._poll_task: asyncio.Task | None = None

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

            artist = track_info.get("artist", "Unknown")
            title = track_info.get("title", "Unknown")
            booth.track_change(artist, title)
            logger.info(f"Track changed: {artist} - {title}")

            # Timeline events
            if self._event_store:
                if self._current_track_event_id is not None:
                    await self._event_store.end_event(self._current_track_event_id)
                self._current_track_event_id = await self._event_store.start_event(
                    event_type="track_play",
                    lane="music",
                    title=f"{artist} \u2014 {title}",
                    details={
                        "filename": current_filename,
                        "artist": artist,
                        "title": title,
                        "duration_seconds": self.remaining_seconds + self.elapsed_seconds,
                    },
                )

            await self._emit("track_changed", track_info)

        # Detect track ending
        if (
            remaining > 0
            and remaining < self.track_ending_threshold
            and not self._track_ending_fired
        ):
            self._track_ending_fired = True
            logger.info(f"Track ending in {remaining:.1f}s")
            await self._emit("track_ending", remaining)

    async def start(self) -> None:
        """Start the background poller."""
        if self._poll_task is not None:
            return
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
