"""
RadioDan Booth Log

The "booth" is where all the action happens - tracking events as they flow
through the system like a DJ tracking their set.

Events:
- 📨 TELEGRAM: Messages from Telegram
- 🎤 TTS: Text-to-speech generation and playback
- 👂 WHISPER: Speech-to-text transcription
- 🔊 MIXER: Audio queue events
- 🎵 TRACK: Music changes
- 🤖 CLAUDE: Claude Code interactions
"""

import logging
import sys
from datetime import datetime
from enum import Enum
from pathlib import Path


class Event(Enum):
    """Event types for the booth log."""
    # Telegram events
    TELEGRAM_IN = "📨 TELEGRAM"
    TELEGRAM_OUT = "📤 REPLY"

    # TTS events
    TTS_REQUEST = "🎤 TTS.REQ"
    TTS_GENERATED = "🎤 TTS.GEN"
    TTS_QUEUED = "🎤 TTS.PLAY"
    TTS_ERROR = "🎤 TTS.ERR"

    # Whisper/STT events
    WHISPER_START = "👂 WHISPER"
    WHISPER_DONE = "👂 HEARD"
    WHISPER_ERROR = "👂 STT.ERR"

    # Mixer events
    MIXER_QUEUE = "🔊 QUEUE"
    MIXER_CONNECT = "🔊 CONNECT"
    MIXER_ERROR = "🔊 MIX.ERR"
    MIXER_VOLUME = "🎚️ VOLUME"
    MIXER_SKIP = "⏭️ SKIP"
    MIXER_FLUSH = "🗑️ FLUSH"
    MIXER_RANDOM = "🔀 RANDOM"

    # LLM events
    LLM_REQUEST = "🤖 LLM"
    LLM_RESPONSE = "🤖 LLM.REPLY"
    LLM_ERROR = "🤖 LLM.ERR"

    # Legacy aliases
    OLLAMA_REQUEST = "🤖 LLM"
    OLLAMA_RESPONSE = "🤖 LLM.REPLY"
    OLLAMA_ERROR = "🤖 LLM.ERR"

    # Claude events
    CLAUDE_QUESTION = "🤖 QUESTION"
    CLAUDE_ANSWER = "🤖 ANSWER"
    CLAUDE_NOTIFY = "🤖 NOTIFY"

    # Track events
    TRACK_CHANGE = "🎵 TRACK"

    # Plugin events
    PLUGIN_START = "🔌 PLUGIN"
    PLUGIN_EVENT = "🔌 EVENT"
    PLUGIN_ERROR = "🔌 PLG.ERR"

    # System events
    SYSTEM_START = "⚡ START"
    SYSTEM_STOP = "⚡ STOP"
    SYSTEM_ERROR = "❌ ERROR"


class BoothFormatter(logging.Formatter):
    """Custom formatter for booth log - clean and DJ-friendly."""

    def format(self, record: logging.LogRecord) -> str:
        # Time in HH:MM:SS format
        timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")

        # Get event type if present, otherwise use level
        event = getattr(record, 'event', None)
        if event:
            prefix = event.value
        else:
            prefix = f"[{record.levelname}]"

        return f"{timestamp} {prefix} │ {record.getMessage()}"


class BoothLog:
    """
    Central event logger for RadioDan.

    Usage:
        from bridge.booth import booth

        booth.telegram("User sent: hello")
        booth.tts_request("Hello world")
        booth.tts_generated("/path/to/audio.wav", 1.2)
    """

    def __init__(self, name: str = "radiodan.booth"):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.INFO)
        self._configured = False

    def configure(self, log_file: Path | None = None, console: bool = True) -> None:
        """Configure booth log outputs."""
        if self._configured:
            return

        formatter = BoothFormatter()

        if console:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)

        if log_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)

        # Don't propagate to root logger (avoid duplicate output)
        self.logger.propagate = False
        self._configured = True

    def _log(self, event: Event, message: str) -> None:
        """Log an event."""
        if not self._configured:
            self.configure()
        self.logger.info(message, extra={'event': event})

    # === Telegram events ===

    def telegram(self, message: str, user: str | None = None) -> None:
        """Log incoming Telegram message."""
        if user:
            self._log(Event.TELEGRAM_IN, f"@{user}: {message}")
        else:
            self._log(Event.TELEGRAM_IN, message)

    def reply(self, message: str) -> None:
        """Log outgoing Telegram reply."""
        self._log(Event.TELEGRAM_OUT, message[:80] + "..." if len(message) > 80 else message)

    # === TTS events ===

    def tts_request(self, text: str, speaker: str = "default") -> None:
        """Log TTS generation request."""
        preview = text[:50] + "..." if len(text) > 50 else text
        self._log(Event.TTS_REQUEST, f'"{preview}" (voice: {speaker})')

    def tts_generated(self, path: str, duration_sec: float | None = None) -> None:
        """Log TTS audio generated."""
        filename = Path(path).name
        if duration_sec:
            self._log(Event.TTS_GENERATED, f"{filename} ({duration_sec:.1f}s)")
        else:
            self._log(Event.TTS_GENERATED, filename)

    def tts_queued(self, path: str) -> None:
        """Log TTS audio queued for playback."""
        filename = Path(path).name
        self._log(Event.TTS_QUEUED, f"▶ {filename}")

    def tts_error(self, error: str) -> None:
        """Log TTS error."""
        self._log(Event.TTS_ERROR, error)

    # === Whisper/STT events ===

    def whisper_start(self, duration_sec: float | None = None) -> None:
        """Log whisper transcription started."""
        if duration_sec:
            self._log(Event.WHISPER_START, f"Transcribing {duration_sec:.1f}s audio...")
        else:
            self._log(Event.WHISPER_START, "Transcribing...")

    def whisper_done(self, text: str) -> None:
        """Log whisper transcription result."""
        preview = text[:60] + "..." if len(text) > 60 else text
        self._log(Event.WHISPER_DONE, f'"{preview}"')

    def whisper_error(self, error: str) -> None:
        """Log whisper error."""
        self._log(Event.WHISPER_ERROR, error)

    # === Mixer events ===

    def mixer_connect(self, host: str, port: int) -> None:
        """Log mixer connection."""
        self._log(Event.MIXER_CONNECT, f"Connected to {host}:{port}")

    def mixer_queue(self, queue_name: str, filename: str) -> None:
        """Log audio queued to mixer."""
        self._log(Event.MIXER_QUEUE, f"{queue_name} ← {filename}")

    def mixer_error(self, error: str) -> None:
        """Log mixer error."""
        self._log(Event.MIXER_ERROR, error)

    def mixer_volume(self, channel: str, value: float, user: str | None = None) -> None:
        """Log volume change."""
        pct = int(value * 100)
        msg = f"{channel}: {pct}%"
        if user:
            msg = f"@{user} → {msg}"
        self._log(Event.MIXER_VOLUME, msg)

    def mixer_skip(self, what: str, user: str | None = None) -> None:
        """Log skip event (next track or skip TTS)."""
        msg = what
        if user:
            msg = f"@{user} → {msg}"
        self._log(Event.MIXER_SKIP, msg)

    def mixer_flush(self, queue: str, user: str | None = None) -> None:
        """Log queue flush."""
        msg = f"{queue} cleared"
        if user:
            msg = f"@{user} → {msg}"
        self._log(Event.MIXER_FLUSH, msg)

    def mixer_random(self, enabled: bool, user: str | None = None) -> None:
        """Log random mode toggle."""
        state = "ON" if enabled else "OFF"
        msg = f"Random: {state}"
        if user:
            msg = f"@{user} → {msg}"
        self._log(Event.MIXER_RANDOM, msg)

    # === LLM events ===

    def llm_request(self, message: str) -> None:
        """Log LLM chat request."""
        preview = message[:60] + "..." if len(message) > 60 else message
        self._log(Event.LLM_REQUEST, f'"{preview}"')

    def llm_response(self, response: str) -> None:
        """Log LLM response."""
        preview = response[:60] + "..." if len(response) > 60 else response
        self._log(Event.LLM_RESPONSE, f'"{preview}"')

    def llm_error(self, error: str) -> None:
        """Log LLM error."""
        self._log(Event.LLM_ERROR, error)

    # Legacy aliases for backwards compatibility
    ollama_request = llm_request
    ollama_response = llm_response
    ollama_error = llm_error

    # === Claude events ===

    def claude_question(self, question: str) -> None:
        """Log question from Claude Code."""
        preview = question[:60] + "..." if len(question) > 60 else question
        self._log(Event.CLAUDE_QUESTION, preview)

    def claude_answer(self, answer: str) -> None:
        """Log answer sent to Claude Code."""
        preview = answer[:60] + "..." if len(answer) > 60 else answer
        self._log(Event.CLAUDE_ANSWER, preview)

    def claude_notify(self, message: str) -> None:
        """Log notification from Claude Code."""
        self._log(Event.CLAUDE_NOTIFY, message)

    # === Track events ===

    def track_change(self, artist: str, title: str) -> None:
        """Log a track change."""
        self._log(Event.TRACK_CHANGE, f"{artist} — {title}")

    # === Plugin events ===

    def plugin_start(self, name: str) -> None:
        """Log plugin started."""
        self._log(Event.PLUGIN_START, f"{name} loaded")

    def plugin_event(self, plugin: str, message: str) -> None:
        """Log plugin activity."""
        self._log(Event.PLUGIN_EVENT, f"[{plugin}] {message}")

    def plugin_error(self, plugin: str, error: str) -> None:
        """Log plugin error."""
        self._log(Event.PLUGIN_ERROR, f"[{plugin}] {error}")

    # === System events ===

    def start(self, component: str) -> None:
        """Log component started."""
        self._log(Event.SYSTEM_START, component)

    def stop(self, component: str) -> None:
        """Log component stopped."""
        self._log(Event.SYSTEM_STOP, component)

    def error(self, message: str) -> None:
        """Log system error."""
        self._log(Event.SYSTEM_ERROR, message)


# Global booth log instance
booth = BoothLog()
