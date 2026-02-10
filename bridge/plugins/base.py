"""
RadioDan Plugin Base

Provides the DJPlugin base class and PluginContext that every plugin receives.
Plugins extend DJPlugin and use self.say() to speak, self.enrich() to share
context, and self.context to read enrichments from other plugins.

Plugins are templates; users create named instances with independent configs.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bridge.audio.stream_context import StreamContext
from bridge.audio.voice_scheduler import VoiceScheduler, VoiceSegment
from bridge.booth import booth

if TYPE_CHECKING:
    from bridge.services.tts_service import TTSService
    from bridge.services.llm_service import LLMService
    from bridge.audio.mixer import LiquidsoapMixer
    from bridge.audio.playlist_planner import PlaylistPlanner

logger = logging.getLogger(__name__)


@dataclass
class TelegramCommand:
    """A Telegram command that a plugin provides."""

    command: str  # e.g. "presenter"
    description: str  # e.g. "Toggle presenter mode"


@dataclass
class TelegramMenuButton:
    """A button to add to the Telegram main menu."""

    label: str  # e.g. "📻 Presenter"
    callback_data: str  # e.g. "plugin:presenter:toggle"


@dataclass
class PluginContext:
    """Everything a plugin needs to operate."""

    tts_service: "TTSService"
    mixer: "LiquidsoapMixer"
    llm_service: "LLMService"
    stream_context: StreamContext
    voice_scheduler: VoiceScheduler
    config: dict = field(default_factory=dict)
    booth: Any = None  # BoothLog instance
    playlist_planner: "PlaylistPlanner | None" = None

    @property
    def ollama_service(self) -> "LLMService":
        """Legacy alias for llm_service."""
        return self.llm_service


class DJPlugin:
    """
    Base class for RadioDan plugins.

    Plugin classes are templates. Users create named instances with
    independent configs. Each instance gets a unique instance_id and
    display_name.

    Subclass this and implement your logic. Use lifecycle hooks
    (on_start, on_stop) and the convenience methods (say, enrich).
    """

    # Override these in subclasses
    name: str = "unnamed"
    description: str = ""
    version: str = "0.1.0"

    def __init__(self, ctx: PluginContext, instance_id: str | None = None, display_name: str | None = None):
        self.ctx = ctx
        self.instance_id = instance_id or f"default-{self.name}"
        self.display_name = display_name or self.name.replace("_", " ").title()
        self.logger = logging.getLogger(f"plugin.{self.name}.{self.instance_id}")
        self._tasks: list[asyncio.Task] = []
        self._running = False

    # =========================================================================
    # CONFIG FIELD DESCRIPTORS
    # =========================================================================

    @classmethod
    def config_fields(cls) -> list[dict]:
        """Describe configurable fields for the web GUI form.

        Returns a list of field descriptors. Each descriptor is a dict with:
          key:     config key name
          type:    text | textarea | number | bool | style_picker
          label:   human-readable label
          default: default value
          help:    optional help text

        Returns [] to use the fallback JSON textarea.
        """
        return []

    # =========================================================================
    # LIFECYCLE
    # =========================================================================

    async def start(self) -> None:
        """Start the plugin. Override on_start() for custom init."""
        self._running = True
        booth.plugin_start(f"{self.name}:{self.instance_id}")
        await self.on_start()

    async def stop(self) -> None:
        """Stop the plugin, cancelling all background tasks."""
        self._running = False
        await self.on_stop()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        self.logger.info(f"Plugin {self.instance_id} stopped")

    async def on_start(self) -> None:
        """Override for custom startup logic."""
        pass

    async def on_stop(self) -> None:
        """Override for custom shutdown logic."""
        pass

    # =========================================================================
    # VOICE OUTPUT
    # =========================================================================

    async def say(self, text: str, **kwargs: Any) -> None:
        """
        Submit text to be spoken on the stream.

        Args:
            text: The text to speak
            **kwargs: VoiceSegment options:
                trigger: "asap", "between_songs", "before_end:X", "after_start:X", "bridge"
                priority: int (lower = first in between-songs queue)
                hold_next_song: bool
                leading_silence: float (seconds)
                trailing_silence: float (seconds)
                pre_generated_audio: Path (skip TTS generation)
                audio_duration: float (for bridge timing math)
                bridge_mix: str ("duck" | "gentle_duck" | "overlay")
        """
        from pathlib import Path

        segment = VoiceSegment(
            text=text,
            trigger=kwargs.get("trigger", "asap"),
            priority=kwargs.get("priority", 0),
            hold_next_song=kwargs.get("hold_next_song", False),
            leading_silence=kwargs.get("leading_silence", 0.0),
            trailing_silence=kwargs.get("trailing_silence", 0.0),
            source_plugin=kwargs.get("lane", self.instance_id),
            pre_generated_audio=kwargs.get("pre_generated_audio"),
            audio_duration=kwargs.get("audio_duration", 0.0),
            bridge_mix=kwargs.get("bridge_mix", "duck"),
            speaker=kwargs.get("speaker"),
            instruct=kwargs.get("instruct"),
        )
        await self.ctx.voice_scheduler.submit(segment)

    # =========================================================================
    # CONTEXT ENRICHMENT
    # =========================================================================

    def enrich(self, key: str, value: Any) -> None:
        """Write a value to the shared enrichment context."""
        self.ctx.stream_context.enrichments[key] = value

    @property
    def context(self) -> dict[str, Any]:
        """Read the shared enrichment context from all plugins."""
        return self.ctx.stream_context.enrichments

    # =========================================================================
    # BACKGROUND TASKS
    # =========================================================================

    def create_task(self, coro: Any) -> asyncio.Task:
        """Create a tracked background task that is cancelled on stop."""
        task = asyncio.create_task(coro)
        self._tasks.append(task)

        def _on_done(t: asyncio.Task) -> None:
            if t in self._tasks:
                self._tasks.remove(t)
            if not t.cancelled() and t.exception():
                self.logger.exception(
                    "Background task failed", exc_info=t.exception()
                )

        task.add_done_callback(_on_done)
        return task

    def run_every(self, interval: float, callback: Any) -> asyncio.Task:
        """Run an async callback periodically."""

        async def _loop() -> None:
            while self._running:
                try:
                    await callback()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.logger.exception(f"Error in periodic task of {self.instance_id}")
                await asyncio.sleep(interval)

        return self.create_task(_loop())

    # =========================================================================
    # TELEGRAM INTEGRATION (optional overrides)
    # =========================================================================

    def telegram_commands(self) -> list[TelegramCommand]:
        """Return Telegram commands this plugin provides. Override to add commands."""
        return []

    def telegram_menu_buttons(self) -> list[TelegramMenuButton]:
        """Return buttons to add to the Telegram main menu. Override to add buttons."""
        return []

    async def handle_telegram_callback(self, action: str) -> str | None:
        """
        Handle a Telegram callback routed to this plugin.

        Args:
            action: The action part of "plugin:<instance_id>:<action>"

        Returns:
            Optional response text to show in Telegram
        """
        return None


class ContextFeeder(DJPlugin):
    """
    Base class for data-providing plugins.

    ContextFeeders don't speak — they provide enrichment data that
    other plugins (like Presenter) can use to make more interesting
    announcements. Data is stored in StreamContext.feeder_context
    which persists across track changes.

    Subclass and override fetch_context() to provide data.
    """

    # Override in subclass to namespace your enrichment keys
    feeder_namespace: str = ""

    @classmethod
    def config_fields(cls) -> list[dict]:
        return [
            {
                "key": "refresh_interval",
                "type": "number",
                "label": "Refresh Interval (seconds)",
                "default": 60,
                "help": "How often to refresh context data (0 = only on start)",
            },
        ]

    async def on_start(self) -> None:
        """Fetch initial data and start periodic refresh."""
        await self._do_fetch()

        interval = self.ctx.config.get("refresh_interval", 60)
        if interval > 0:
            self.run_every(interval, self._do_fetch)

    async def _do_fetch(self) -> None:
        """Fetch context data and store in feeder_context."""
        namespace = self.feeder_namespace or self.name

        try:
            data = await self.fetch_context()
            for key, value in data.items():
                self.ctx.stream_context.feeder_context[f"{namespace}.{key}"] = value
        except Exception:
            self.logger.exception(f"Failed to fetch context for {namespace}")

    async def fetch_context(self) -> dict[str, Any]:
        """Override in subclass to provide context data.

        Returns:
            Dict of key -> value pairs to store in feeder_context.
            Keys will be prefixed with the feeder_namespace.
        """
        return {}
