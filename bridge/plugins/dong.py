"""
RadioDan DONG Plugin — Time-Based Announcements

Fires announcements based on time/schedule. Three mutually exclusive modes:

  recurring    — Hourly on the dot, or daily at a set time
  oneshot      — Fire once at a specific datetime
  between_songs — Fire on every track change

Default: recurring hourly, saying "Dooong! The time is HH:MM"
"""

import asyncio
import logging
from datetime import datetime, timedelta

from bridge.plugins import register_plugin
from bridge.plugins.base import DJPlugin, TelegramCommand, TelegramMenuButton

logger = logging.getLogger(__name__)


@register_plugin
class DongPlugin(DJPlugin):
    """Time-based announcements — hourly chimes, scheduled alerts, per-song."""

    name = "dong"
    description = "Time-based announcements — hourly chimes, scheduled alerts, per-song"
    version = "0.1.0"

    @classmethod
    def config_fields(cls) -> list[dict]:
        return [
            {
                "key": "active_on_start",
                "type": "bool",
                "label": "Active on Start",
                "default": True,
                "help": "Start announcing immediately when the plugin loads",
            },
            {
                "key": "mode",
                "type": "select",
                "label": "Mode",
                "default": "recurring",
                "options": [
                    {"value": "recurring", "label": "Recurring"},
                    {"value": "oneshot", "label": "One-shot"},
                    {"value": "between_songs", "label": "Between every song"},
                ],
                "help": "When to fire announcements",
            },
            {
                "key": "recurring_type",
                "type": "select",
                "label": "Recurring Schedule",
                "default": "hourly",
                "options": [
                    {"value": "hourly", "label": "Hourly (on the dot)"},
                    {"value": "daily", "label": "Daily (at set time)"},
                ],
                "show_when": {"field": "mode", "value": "recurring"},
                "help": "How often to fire in recurring mode",
            },
            {
                "key": "daily_time",
                "type": "text",
                "label": "Daily Time (HH:MM)",
                "default": "12:00",
                "show_when": {"field": "recurring_type", "value": "daily"},
                "help": "Time of day for daily announcements (24h format)",
            },
            {
                "key": "oneshot_datetime",
                "type": "datetime",
                "label": "Fire At",
                "default": "",
                "show_when": {"field": "mode", "value": "oneshot"},
                "help": "Exact date/time for the one-shot announcement",
            },
            {
                "key": "say_text",
                "type": "text",
                "label": "Say Text",
                "default": "Dooong! The time is {time}",
                "help": "Text to speak. Use {time} for current HH:MM. Leave empty to use LLM prompt instead.",
            },
            {
                "key": "prompt",
                "type": "textarea",
                "label": "LLM Prompt (fallback)",
                "default": "",
                "help": "If Say Text is empty, this prompt is sent to the LLM. Use {time} for current HH:MM.",
            },
        ]

    def __init__(self, ctx, instance_id=None, display_name=None) -> None:
        super().__init__(ctx, instance_id=instance_id, display_name=display_name)
        self._active = False

    async def on_start(self) -> None:
        cfg = self.ctx.config
        self._active = cfg.get("active_on_start", True)
        self._mode = cfg.get("mode", "recurring")
        self._say_text = cfg.get("say_text", "Dooong! The time is {time}")
        self._prompt = cfg.get("prompt", "")

        if not self._active:
            self.logger.info(f"Dong '{self.instance_id}' started in standby (inactive)")
            return

        if self._mode == "recurring":
            recurring_type = cfg.get("recurring_type", "hourly")
            if recurring_type == "hourly":
                self.create_task(self._clock_aligned_loop(60, target_minute=0))
                self.logger.info(f"Dong '{self.instance_id}' started: recurring hourly")
            elif recurring_type == "daily":
                daily_time = cfg.get("daily_time", "12:00")
                try:
                    parts = daily_time.strip().split(":")
                    hour, minute = int(parts[0]), int(parts[1])
                except (ValueError, IndexError):
                    hour, minute = 12, 0
                    self.logger.warning(f"Invalid daily_time '{daily_time}', defaulting to 12:00")
                self.create_task(self._daily_loop(hour, minute))
                self.logger.info(f"Dong '{self.instance_id}' started: recurring daily at {hour:02d}:{minute:02d}")

        elif self._mode == "oneshot":
            dt_str = cfg.get("oneshot_datetime", "")
            if dt_str:
                self.create_task(self._oneshot_fire(dt_str))
                self.logger.info(f"Dong '{self.instance_id}' started: one-shot at {dt_str}")
            else:
                self.logger.warning(f"Dong '{self.instance_id}': oneshot mode but no datetime configured")

        elif self._mode == "between_songs":
            self.ctx.stream_context.on("track_changed", self._on_track_changed)
            self.logger.info(f"Dong '{self.instance_id}' started: between every song")

    # =========================================================================
    # SCHEDULING
    # =========================================================================

    async def _clock_aligned_loop(self, interval_minutes: int, target_minute: int = 0) -> None:
        """Fire at clock-aligned intervals (e.g. every hour on the dot)."""
        while self._running:
            now = datetime.now()
            next_fire = now.replace(minute=target_minute, second=0, microsecond=0)
            if next_fire <= now:
                next_fire += timedelta(minutes=interval_minutes)
            delay = (next_fire - now).total_seconds()
            self.logger.debug(f"Next hourly dong in {delay:.0f}s at {next_fire.strftime('%H:%M')}")
            await asyncio.sleep(delay)
            if self._active and self._running:
                await self._fire_announcement()

    async def _daily_loop(self, hour: int, minute: int) -> None:
        """Fire once daily at the specified time."""
        while self._running:
            now = datetime.now()
            next_fire = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_fire <= now:
                next_fire += timedelta(days=1)
            delay = (next_fire - now).total_seconds()
            self.logger.debug(f"Next daily dong in {delay:.0f}s at {next_fire.strftime('%Y-%m-%d %H:%M')}")
            await asyncio.sleep(delay)
            if self._active and self._running:
                await self._fire_announcement()

    async def _oneshot_fire(self, dt_str: str) -> None:
        """Fire once at a specific datetime."""
        try:
            target = datetime.fromisoformat(dt_str)
        except ValueError:
            self.logger.error(f"Invalid oneshot datetime: {dt_str}")
            return

        delay = (target - datetime.now()).total_seconds()
        if delay <= 0:
            self.logger.warning(f"Oneshot datetime is in the past: {dt_str}")
            return

        self.logger.info(f"Oneshot dong scheduled in {delay:.0f}s")
        await asyncio.sleep(delay)
        if self._active and self._running:
            await self._fire_announcement()

    async def _on_track_changed(self, track_info: dict) -> None:
        """Fire on every track change (between_songs mode)."""
        if self._active:
            await self._fire_announcement()

    # =========================================================================
    # ANNOUNCEMENT
    # =========================================================================

    async def _fire_announcement(self) -> None:
        """Generate and speak the announcement."""
        time_str = datetime.now().strftime("%H:%M")

        try:
            if self._say_text:
                text = self._say_text.replace("{time}", time_str)
            elif self._prompt:
                prompt_filled = self._prompt.replace("{time}", time_str)
                text = await self.ctx.llm_service.chat(prompt_filled)
            else:
                text = f"The time is {time_str}"

            await self.say(text, trigger="between_songs", priority=30, lane="time")
            self.logger.info(f"Dong fired: {text[:60]}")
        except Exception:
            self.logger.exception("Failed to fire dong announcement")

    # =========================================================================
    # TELEGRAM INTEGRATION
    # =========================================================================

    def telegram_commands(self) -> list[TelegramCommand]:
        return [TelegramCommand("dong", f"Toggle {self.display_name}")]

    def telegram_menu_buttons(self) -> list[TelegramMenuButton]:
        label = f"🔔 {self.display_name} ON" if self._active else f"🔔 {self.display_name}"
        return [TelegramMenuButton(label, f"plugin:{self.instance_id}:toggle")]

    async def handle_telegram_callback(self, action: str) -> str | None:
        if action in ("toggle", "command"):
            self._active = not self._active
            state = "ON" if self._active else "OFF"
            self.logger.info(f"Dong '{self.instance_id}' toggled: {state}")

            from bridge.booth import booth
            booth.plugin_event(self.instance_id, f"Toggled {state}")

            return (
                f"🔔 *{self.display_name}: {state}*\n\n"
                + (
                    f"Mode: {self._mode}\n"
                    f"Text: {self._say_text[:50] or '(LLM prompt)'}\n\n"
                    "Time announcements are active."
                    if self._active
                    else "Time announcements paused. Tap again to re-enable."
                )
            )
        return None
