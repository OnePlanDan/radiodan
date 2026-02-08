"""
RadioDan Configuration Loader

Loads configuration from:
1. stations/<name>/station.yaml - Station-specific settings (if RADIODAN_STATION_DIR set)
   OR config/radiodan.yaml - Legacy single-station mode
2. .env file - Secrets (tokens, passwords)
"""

import os
from pathlib import Path
from dataclasses import dataclass, field

import yaml
from dotenv import load_dotenv


@dataclass
class IcecastConfig:
    host: str = "icecast"
    port: int = 8000
    mount: str = "/stream"
    external_port: int = 49994


@dataclass
class LiquidsoapConfig:
    telnet_host: str = "liquidsoap"
    telnet_port: int = 1234
    crossfade_duration: float = 5.0


@dataclass
class PlaylistConfig:
    music_dir: str = "./music"
    lookahead: int = 5
    scan_interval: float = 300.0


@dataclass
class TTSConfig:
    endpoint: str = "http://localhost:42001/tts/custom-voice"
    speaker: str = "Aiden"
    language: str = "English"
    instruct: str = "Speak calmly and clearly"
    cache_dir: str = "/tmp/tts_cache"


@dataclass
class STTConfig:
    endpoint: str = "http://localhost:5000/v1/audio/transcriptions"


@dataclass
class OllamaConfig:
    endpoint: str = "http://localhost:11434/v1/chat/completions"
    model: str = "gpt-oss:20b"
    system_prompt: str = "You are {station_name}, a friendly AI assistant. Keep responses concise (1-2 sentences) since they'll be spoken aloud."


@dataclass
class TelegramConfig:
    enabled: bool = True
    token: str = ""
    allowed_users: list[int] = field(default_factory=list)


@dataclass
class AudioConfig:
    icecast: IcecastConfig = field(default_factory=IcecastConfig)
    liquidsoap: LiquidsoapConfig = field(default_factory=LiquidsoapConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    playlist: PlaylistConfig = field(default_factory=PlaylistConfig)


@dataclass
class AIConfig:
    ollama: OllamaConfig = field(default_factory=OllamaConfig)


@dataclass
class Config:
    """Main configuration container."""
    station_name: str = "Radio Dan"
    audio: AudioConfig = field(default_factory=AudioConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    ai: AIConfig = field(default_factory=AIConfig)
    plugins: dict = field(default_factory=dict)

    @classmethod
    def load(cls, config_dir: Path | None = None) -> "Config":
        """Load configuration from yaml file and environment variables.

        Resolution order:
        1. RADIODAN_STATION_DIR env var → station_dir/station.yaml
        2. Explicit config_dir argument → config_dir/radiodan.yaml (legacy)
        3. Default: ../config/radiodan.yaml (legacy)
        """
        station_dir = os.environ.get("RADIODAN_STATION_DIR")

        if station_dir:
            # Station mode: load from station directory
            yaml_path = Path(station_dir) / "station.yaml"
            load_dotenv(Path(station_dir) / ".env", override=False)
        else:
            # Legacy mode: use config_dir
            if config_dir is None:
                config_dir = Path(__file__).parent.parent / "config"
            yaml_path = config_dir / "radiodan.yaml"
            load_dotenv(config_dir.parent / ".env")

        # Load yaml config
        yaml_config = {}
        if yaml_path.exists():
            with open(yaml_path) as f:
                yaml_config = yaml.safe_load(f) or {}

        # Station name (top-level config)
        station_name = yaml_config.get("station_name", "Radio Dan")

        # Build config objects
        audio_cfg = yaml_config.get("audio", {})
        icecast_cfg = audio_cfg.get("icecast", {})
        liquidsoap_cfg = audio_cfg.get("liquidsoap", {})
        tts_cfg = audio_cfg.get("tts", {})

        icecast = IcecastConfig(
            host=icecast_cfg.get("host", "icecast"),
            port=icecast_cfg.get("port", 8000),
            mount=icecast_cfg.get("mount", "/stream"),
            external_port=icecast_cfg.get("external_port", 49994),
        )

        liquidsoap = LiquidsoapConfig(
            telnet_host=liquidsoap_cfg.get("telnet_host", "liquidsoap"),
            telnet_port=liquidsoap_cfg.get("telnet_port", 1234),
            crossfade_duration=liquidsoap_cfg.get("crossfade_duration", 5.0),
        )

        playlist_cfg = audio_cfg.get("playlist", {})
        playlist = PlaylistConfig(
            music_dir=playlist_cfg.get("music_dir", "./music"),
            lookahead=playlist_cfg.get("lookahead", 5),
            scan_interval=playlist_cfg.get("scan_interval", 300.0),
        )

        # Env vars override yaml for deployment-specific endpoints
        tts = TTSConfig(
            endpoint=os.getenv("TTS_ENDPOINT", tts_cfg.get("endpoint", "http://localhost:42001/tts/custom-voice")),
            speaker=tts_cfg.get("speaker", "Aiden"),
            language=tts_cfg.get("language", "English"),
            instruct=tts_cfg.get("instruct", "Speak calmly and clearly"),
            cache_dir=tts_cfg.get("cache_dir", "/tmp/tts_cache"),
        )

        stt_cfg = audio_cfg.get("stt", {})
        stt = STTConfig(
            endpoint=os.getenv("STT_ENDPOINT", stt_cfg.get("endpoint", "http://localhost:5000/v1/audio/transcriptions")),
        )

        # Ollama/AI config — interpolate station_name into system_prompt
        ollama_cfg = yaml_config.get("ollama", {})
        default_prompt = f"You are {station_name}, a friendly AI assistant. Keep responses concise (1-2 sentences) since they'll be spoken aloud."
        ollama = OllamaConfig(
            endpoint=os.getenv("OLLAMA_ENDPOINT", ollama_cfg.get("endpoint", "http://localhost:11434/v1/chat/completions")),
            model=os.getenv("OLLAMA_MODEL", ollama_cfg.get("model", "gpt-oss:20b")),
            system_prompt=ollama_cfg.get("system_prompt", default_prompt),
        )

        # Telegram config from environment
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        user_id_str = os.getenv("TELEGRAM_USER_ID", "")
        allowed_users = []
        if user_id_str:
            try:
                allowed_users = [int(uid.strip()) for uid in user_id_str.split(",")]
            except ValueError:
                pass

        telegram = TelegramConfig(
            enabled=yaml_config.get("channels", {}).get("telegram", {}).get("enabled", True),
            token=token,
            allowed_users=allowed_users,
        )

        # Plugin configs
        plugins = yaml_config.get("plugins", {})

        return cls(
            station_name=station_name,
            audio=AudioConfig(icecast=icecast, liquidsoap=liquidsoap, tts=tts, stt=stt, playlist=playlist),
            telegram=telegram,
            ai=AIConfig(ollama=ollama),
            plugins=plugins,
        )


def get_stream_url(config: Config, local_ip: str | None = None) -> str:
    """Generate the stream URL for users to connect to."""
    if local_ip is None:
        local_ip = "localhost"
    return f"http://{local_ip}:{config.audio.icecast.external_port}{config.audio.icecast.mount}"
