"""
RadioDan Presenter Plugin

A radio-style presenter that announces tracks and provides ambient
commentary. Supports 4 DJ styles with weighted random selection:

  intro     -- Talk over the beginning of a new song (asap)
  outro     -- Talk as a song fades out (before_end)
  mid_song  -- Drop in with a comment mid-song (after_start, random)
  silent    -- Skip, create breathing room

Each instance can have its own persona, style mix, and prompt templates.
Style-awareness prevents double-talking (e.g., a track that got an intro
won't also get an outro at its end).
"""

import logging
import random

from bridge.plugins import register_plugin
from bridge.plugins.base import DJPlugin, TelegramCommand, TelegramMenuButton

logger = logging.getLogger(__name__)

DEFAULT_PERSONA_NAME = "Radio Dan"

DEFAULT_SYSTEM_PROMPT = (
    "You are {persona_name}, a chill and friendly radio presenter on an ambient music stream. "
    "Keep announcements brief (1-3 sentences). Be warm, conversational, and occasionally witty. "
    "Never use hashtags, emojis, or markdown formatting -- your words will be spoken aloud. "
    "If given song info, weave it naturally into your announcement."
)

# Style-specific prompt templates (defaults, editable per instance)
DEFAULT_STYLE_PROMPTS = {
    "intro": (
        "Announce this song that just started playing on the radio. "
        "Here's what you know:\n{context}"
    ),
    "outro": (
        "The song {title} by {artist} is wrapping up. "
        "Give a brief, warm sendoff as the song fades out. Keep it to 1-2 sentences."
    ),
    "mid_song": (
        "We're listening to {title} by {artist}. "
        "Drop in with a brief, interesting comment about the song, artist, or vibe. "
        "Keep it to 1 sentence -- don't interrupt the flow too much."
    ),
}

# Default style weights (equal-ish, with silent providing breathing room)
DEFAULT_STYLE_WEIGHTS = {
    "intro": 3,
    "outro": 2,
    "mid_song": 1,
    "silent": 1,
}

ALL_STYLES = ["intro", "outro", "mid_song", "silent"]


@register_plugin
class PresenterPlugin(DJPlugin):
    """Radio-style DJ with 4 announcement styles and per-instance config."""

    name = "presenter"
    description = "Radio-style DJ announcements with multiple styles"
    version = "0.3.0"

    @classmethod
    def config_fields(cls) -> list[dict]:
        return [
            {
                "key": "persona_name",
                "type": "text",
                "label": "Persona Name",
                "default": DEFAULT_PERSONA_NAME,
                "help": "The DJ's on-air name",
            },
            {
                "key": "styles",
                "type": "style_picker",
                "label": "DJ Styles",
                "options": [
                    {"value": "intro", "label": "Intro", "desc": "Talk over beginning of new song", "default_weight": 3},
                    {"value": "outro", "label": "Outro", "desc": "Talk as song fades out", "default_weight": 2},
                    {"value": "mid_song", "label": "Mid-Song", "desc": "Drop in with a comment mid-song", "default_weight": 1},
                    {"value": "silent", "label": "Silent", "desc": "Skip -- create breathing room", "default_weight": 1},
                ],
            },
            {
                "key": "system_prompt",
                "type": "textarea",
                "label": "System Prompt",
                "default": DEFAULT_SYSTEM_PROMPT,
                "help": "The system prompt sent to the LLM. Use {persona_name} as a placeholder.",
            },
            {
                "key": "periodic_interval",
                "type": "number",
                "label": "Periodic Interval (seconds, 0=off)",
                "default": 0,
                "help": "Seconds between periodic ambient announcements (0 to disable)",
            },
            {
                "key": "outro_before_end",
                "type": "number",
                "label": "Outro Lead Time (seconds)",
                "default": 30,
                "help": "How many seconds before song end to trigger outro",
            },
            {
                "key": "mid_song_min",
                "type": "number",
                "label": "Mid-Song Earliest (seconds)",
                "default": 30,
                "help": "Earliest point in a song for a mid-song comment",
            },
            {
                "key": "mid_song_max",
                "type": "number",
                "label": "Mid-Song Latest (seconds)",
                "default": 120,
                "help": "Latest point in a song for a mid-song comment",
            },
            {
                "key": "style_prompts.intro",
                "type": "textarea",
                "label": "Intro Prompt",
                "default": DEFAULT_STYLE_PROMPTS["intro"],
                "help": "Prompt template for intro announcements. Variables: {context}, {artist}, {title}, {year}, {genre}",
            },
            {
                "key": "style_prompts.outro",
                "type": "textarea",
                "label": "Outro Prompt",
                "default": DEFAULT_STYLE_PROMPTS["outro"],
                "help": "Prompt template for outro. Variables: {title}, {artist}",
            },
            {
                "key": "style_prompts.mid_song",
                "type": "textarea",
                "label": "Mid-Song Prompt",
                "default": DEFAULT_STYLE_PROMPTS["mid_song"],
                "help": "Prompt template for mid-song. Variables: {title}, {artist}",
            },
        ]

    def __init__(self, ctx, instance_id=None, display_name=None) -> None:
        super().__init__(ctx, instance_id=instance_id, display_name=display_name)
        self._active = True
        self._prev_track: dict | None = None
        self._prev_style: str | None = None

    async def on_start(self) -> None:
        """Subscribe to track changes and configure styles."""
        cfg = self.ctx.config

        # Core settings
        self._periodic_interval = cfg.get("periodic_interval", 0)

        # Persona
        self._persona_name = cfg.get("persona_name", DEFAULT_PERSONA_NAME)
        raw_prompt = cfg.get("system_prompt", DEFAULT_SYSTEM_PROMPT)
        self._system_prompt = raw_prompt.replace("{persona_name}", self._persona_name)

        # Styles
        self._styles = cfg.get("styles", list(DEFAULT_STYLE_WEIGHTS.keys()))
        # Validate styles
        self._styles = [s for s in self._styles if s in ALL_STYLES]
        if not self._styles:
            self._styles = ["intro", "silent"]

        # Style weights
        configured_weights = cfg.get("style_weights", {})
        self._style_weights = {
            s: configured_weights.get(s, DEFAULT_STYLE_WEIGHTS.get(s, 1))
            for s in self._styles
        }

        # Style-specific prompt templates
        self._style_prompts = dict(DEFAULT_STYLE_PROMPTS)
        custom_prompts = cfg.get("style_prompts", {})
        self._style_prompts.update(custom_prompts)

        # Mid-song timing range (seconds into song)
        self._mid_song_min = cfg.get("mid_song_min", 30)
        self._mid_song_max = cfg.get("mid_song_max", 120)

        # Outro timing (seconds before end)
        self._outro_before_end = cfg.get("outro_before_end", 30)

        # Subscribe to stream events
        self.ctx.stream_context.on("track_changed", self._on_track_changed)

        # Start periodic announcements if configured
        if self._periodic_interval > 0:
            self.run_every(self._periodic_interval, self._periodic_announce)

        state = "active" if self._active else "standby"
        styles_str = ", ".join(self._styles)
        self.logger.info(f"Presenter '{self.instance_id}' started ({state}) -- styles: [{styles_str}]")

    def _pick_style(self) -> str:
        """Pick a random style using configured weights.

        Excludes "outro" if the previous track got an "intro" to avoid
        double-talking at the same transition boundary.
        """
        exclude = set()
        if self._prev_style == "intro":
            exclude.add("outro")

        styles = [s for s in self._style_weights if s not in exclude]
        if not styles:
            styles = list(self._style_weights.keys())
        weights = [self._style_weights[s] for s in styles]
        return random.choices(styles, weights=weights, k=1)[0]

    def _build_track_context(self, track_info: dict) -> dict:
        """Build template variables from track info + enrichments."""
        ctx = {
            "artist": track_info.get("artist", "").strip() or "Unknown Artist",
            "title": track_info.get("title", "").strip() or "Unknown Title",
            "year": track_info.get("year", "").strip(),
            "genre": track_info.get("genre", "").strip(),
            "persona_name": self._persona_name,
            "prev_artist": "",
            "prev_title": "",
        }

        if self._prev_track:
            ctx["prev_artist"] = self._prev_track.get("artist", "").strip() or "Unknown Artist"
            ctx["prev_title"] = self._prev_track.get("title", "").strip() or "Unknown Title"

        return ctx

    def _build_context_block(self, track_info: dict) -> str:
        """Build a multi-line context block for the intro prompt."""
        parts = []
        artist = track_info.get("artist", "").strip()
        title = track_info.get("title", "").strip()
        year = track_info.get("year", "").strip()
        genre = track_info.get("genre", "").strip()

        if artist:
            parts.append(f"Artist: {artist}")
        if title:
            parts.append(f"Title: {title}")
        if year:
            parts.append(f"Year: {year}")
        if genre:
            parts.append(f"Genre: {genre}")

        # Check for enrichments from other plugins
        lyrics_snippet = self.context.get("lyrics", "")
        if lyrics_snippet:
            parts.append(f"Lyrics snippet: {lyrics_snippet[:200]}")

        geo_info = self.context.get("geo", "")
        if geo_info:
            parts.append(f"Geographic context: {geo_info}")

        # Include feeder context if available
        feeder = self.ctx.stream_context.feeder_context
        for key, value in feeder.items():
            if value:
                parts.append(f"{key}: {str(value)[:100]}")

        return "\n".join(parts)

    # =========================================================================
    # TRACK CHANGE HANDLING
    # =========================================================================

    async def _on_track_changed(self, track_info: dict) -> None:
        """Pick a style and generate an announcement when a new track starts."""
        if not self._active:
            self._prev_track = track_info
            return

        artist = track_info.get("artist", "").strip()
        title = track_info.get("title", "").strip()

        if not artist and not title:
            self._prev_track = track_info
            return

        style = self._pick_style()
        self.logger.info(f"Style chosen: {style} for '{artist} - {title}'")

        try:
            if style == "silent":
                self.logger.info("Silent style -- skipping announcement")

            elif style == "intro":
                await self._announce_intro(track_info)

            elif style == "outro":
                # Register a before_end handler for the *current* song
                self._schedule_outro(track_info)

            elif style == "mid_song":
                self._schedule_mid_song(track_info)

        except Exception:
            self.logger.exception(f"Failed to generate {style} announcement")

        self._prev_style = style
        self._prev_track = track_info

    async def _announce_intro(self, track_info: dict) -> None:
        """Intro style: announce the song that just started (asap trigger)."""
        ctx = self._build_track_context(track_info)
        ctx["context"] = self._build_context_block(track_info)

        prompt_template = self._style_prompts.get("intro", DEFAULT_STYLE_PROMPTS["intro"])
        prompt = prompt_template.format(**ctx)

        announcement = await self.ctx.llm_service.chat(prompt, system_prompt=self._system_prompt)
        await self.say(
            announcement,
            trigger="asap",
            priority=50,
            leading_silence=0.5,
            trailing_silence=0.3,
        )

    def _schedule_outro(self, track_info: dict) -> None:
        """Outro style: schedule an announcement before the song ends."""
        async def _generate_outro():
            ctx = self._build_track_context(track_info)
            prompt_template = self._style_prompts.get("outro", DEFAULT_STYLE_PROMPTS["outro"])
            prompt = prompt_template.format(**ctx)

            announcement = await self.ctx.llm_service.chat(prompt, system_prompt=self._system_prompt)
            await self.say(
                announcement,
                trigger=f"before_end:{self._outro_before_end}",
                priority=40,
                leading_silence=0.2,
                trailing_silence=0.2,
            )

        self.create_task(_generate_outro())

    def _schedule_mid_song(self, track_info: dict) -> None:
        """Mid-song style: schedule a comment at a random point during playback."""
        delay = random.randint(self._mid_song_min, self._mid_song_max)

        async def _generate_mid_song():
            ctx = self._build_track_context(track_info)
            prompt_template = self._style_prompts.get("mid_song", DEFAULT_STYLE_PROMPTS["mid_song"])
            prompt = prompt_template.format(**ctx)

            announcement = await self.ctx.llm_service.chat(prompt, system_prompt=self._system_prompt)
            await self.say(
                announcement,
                trigger=f"after_start:{delay}",
                priority=70,
                leading_silence=0.3,
                trailing_silence=0.2,
            )

        self.create_task(_generate_mid_song())

    async def _periodic_announce(self) -> None:
        """Generate a periodic ambient announcement."""
        if not self._active:
            return

        track = self.ctx.stream_context.current_track
        artist = track.get("artist", "").strip()
        title = track.get("title", "").strip()

        context_parts = []
        if artist and title:
            context_parts.append(f"Currently playing: {artist} -- {title}")

        for key, value in self.context.items():
            if value:
                context_parts.append(f"{key}: {str(value)[:100]}")

        prompt = (
            "Give a brief ambient radio interlude. "
            "Maybe mention the time, the vibe, or something interesting. "
            + ("\n".join(context_parts) if context_parts else "No specific context available.")
        )

        try:
            announcement = await self.ctx.llm_service.chat(prompt, system_prompt=self._system_prompt)
            await self.say(
                announcement,
                trigger="between_songs",
                priority=90,
                leading_silence=0.5,
            )
        except Exception:
            self.logger.exception("Failed to generate periodic announcement")

    # =========================================================================
    # TELEGRAM INTEGRATION
    # =========================================================================

    def telegram_commands(self) -> list[TelegramCommand]:
        return [TelegramCommand("presenter", f"Toggle {self.display_name}")]

    def telegram_menu_buttons(self) -> list[TelegramMenuButton]:
        label = f"📻 {self.display_name} ON" if self._active else f"📻 {self.display_name}"
        return [TelegramMenuButton(label, f"plugin:{self.instance_id}:toggle")]

    async def handle_telegram_callback(self, action: str) -> str | None:
        if action in ("toggle", "command"):
            self._active = not self._active
            state = "ON" if self._active else "OFF"
            self.logger.info(f"Presenter '{self.instance_id}' toggled: {state}")

            from bridge.booth import booth
            booth.plugin_event(self.instance_id, f"Toggled {state}")

            styles_str = ", ".join(self._styles)
            return (
                f"📻 *{self.display_name}: {state}*\n\n"
                + (
                    f"Styles: {styles_str}\n"
                    f"Persona: {self._persona_name}\n\n"
                    "I'll announce tracks as they change."
                    if self._active
                    else "Presenter is now quiet. Tap again to re-enable."
                )
            )
        return None
