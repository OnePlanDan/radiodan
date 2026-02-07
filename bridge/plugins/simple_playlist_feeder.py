"""
Simple Playlist Feeder Plugin

Provides random track selection with no-repeat protection.
Registers as the playlist feeder via PlaylistPlanner.set_feeder(),
implementing the SelectionStrategy protocol.

This replaces the old built-in RandomStrategy that was hardcoded
in PlaylistPlanner, making track selection configurable and swappable.
"""

import random

from bridge.plugins import register_plugin
from bridge.plugins.base import DJPlugin


@register_plugin
class SimplePlaylistFeeder(DJPlugin):
    """Random track selection with configurable no-repeat protection."""

    name = "simple_playlist_feeder"
    description = "Random track selection with no-repeat protection"
    version = "0.1.0"

    @classmethod
    def config_fields(cls) -> list[dict]:
        return [
            {
                "key": "no_repeat_count",
                "type": "number",
                "label": "No-Repeat Count",
                "default": 10,
                "help": "Number of recently played tracks to exclude from selection.",
            },
        ]

    async def on_start(self) -> None:
        self._no_repeat_count = self.ctx.config.get("no_repeat_count", 10)
        if self.ctx.playlist_planner:
            self.ctx.playlist_planner.set_feeder(self)
            self.logger.info(f"Registered as feeder (no_repeat_count={self._no_repeat_count})")
        else:
            self.logger.warning("No playlist planner available — feeder not registered")

    async def on_stop(self) -> None:
        if self.ctx.playlist_planner:
            self.ctx.playlist_planner.clear_feeder()

    async def select_next(
        self,
        library: list[dict],
        history: list[dict],
        upcoming: list[dict],
    ) -> dict | None:
        """Select a random track, excluding recent history and upcoming queue."""
        if not library:
            return None

        # Build exclusion set from recent history + upcoming queue
        recent_paths = {h["file_path"] for h in history[: self._no_repeat_count]}
        upcoming_paths = {t["file_path"] for t in upcoming}
        exclude = recent_paths | upcoming_paths

        # Filter candidates
        candidates = [t for t in library if t["file_path"] not in exclude]

        # If all tracks are excluded (small library), allow repeats from upcoming
        if not candidates:
            candidates = [t for t in library if t["file_path"] not in recent_paths]

        # Last resort: pick from entire library
        if not candidates:
            candidates = library

        return random.choice(candidates)
