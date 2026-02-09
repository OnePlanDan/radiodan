#!/usr/bin/env python3
"""
RadioDan Bridge Service

Main entry point for the RadioDan system.
Starts the Telegram bot, web GUI, and coordinates with audio streaming.
"""

import asyncio
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path

from bridge.config import Config, get_stream_url
from bridge.config_store import ConfigStore
from bridge.event_store import EventStore
from bridge.channels.telegram import TelegramChannel
from bridge.services.tts_service import TTSService
from bridge.services.stt_service import STTService
from bridge.services.llm_service import LLMService
from bridge.audio.mixer import LiquidsoapMixer
from bridge.audio.stream_context import StreamContext
from bridge.audio.voice_scheduler import VoiceScheduler
from bridge.audio.playlist_planner import PlaylistPlanner
from bridge.plugins import load_plugin_instances
from bridge.web.server import WebServer
from bridge.booth import booth

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("radiodan")

def get_local_ip() -> str:
    """Get the local IP address for LAN access."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


async def main() -> None:
    """Main application entry point."""
    logger.info("=" * 50)
    logger.info("RadioDan Bridge Service starting...")
    logger.info("=" * 50)

    # Determine station directory
    station_dir_env = os.environ.get("RADIODAN_STATION_DIR")
    if station_dir_env:
        station_dir = Path(station_dir_env)
    else:
        # Legacy fallback: use config/ directory
        station_dir = Path(__file__).parent.parent / "config"

    # Configure booth log (DJ event log)
    booth_log_file = Path(__file__).parent.parent / "logs" / "booth.log"
    booth.configure(log_file=booth_log_file, console=True)

    # Load configuration (reads RADIODAN_STATION_DIR internally)
    config = Config.load()

    station_name = config.station_name
    booth.start(station_name)

    # Open SQLite config store (DB lives in station dir)
    config_store = ConfigStore()
    db_path = station_dir / "radiodan.db"
    await config_store.open(db_path)
    logger.info(f"Config store opened: {db_path}")

    # Open event store (timeline persistence, shares same DB)
    event_store = EventStore(db_path)
    await event_store.open()

    # Determine stream URL
    local_ip = get_local_ip()
    stream_url = get_stream_url(config, local_ip)
    logger.info(f"Stream URL: {stream_url}")

    # Validate Telegram configuration
    if not config.telegram.token:
        logger.error("TELEGRAM_BOT_TOKEN not set in .env file!")
        logger.error("Please copy .env.example to .env and configure your token.")
        sys.exit(1)

    if not config.telegram.allowed_users:
        logger.warning("No TELEGRAM_USER_ID configured - bot will accept all users!")
    else:
        logger.info(f"Allowed Telegram users: {config.telegram.allowed_users}")

    # Initialize TTS service
    tts_cache_dir = Path(__file__).parent.parent / "tmp" / "tts_cache"
    tts_cache_dir.mkdir(parents=True, exist_ok=True)

    tts_service = TTSService(
        endpoint=config.audio.tts.endpoint,
        cache_dir=tts_cache_dir,
        speaker=config.audio.tts.speaker,
        language=config.audio.tts.language,
        instruct=config.audio.tts.instruct,
    )
    logger.info(f"TTS service configured (endpoint: {config.audio.tts.endpoint})")

    # Initialize STT (Speech-to-Text) service
    stt_service = STTService(endpoint=config.audio.stt.endpoint)
    logger.info(f"STT service configured (endpoint: {config.audio.stt.endpoint})")

    # Initialize LLM service
    llm_service = LLMService(
        endpoint=config.ai.ollama.endpoint,
        model=config.ai.ollama.model,
        system_prompt=config.ai.ollama.system_prompt,
    )
    logger.info(f"LLM service configured (endpoint: {config.ai.ollama.endpoint}, model: {config.ai.ollama.model})")

    # Initialize Liquidsoap mixer
    project_root = Path(__file__).parent.parent
    mixer = LiquidsoapMixer(
        host=config.audio.liquidsoap.telnet_host,
        port=config.audio.liquidsoap.telnet_port,
        path_mappings={
            project_root / "music": "/music",
            project_root / "tmp": "/tmp",
        },
        config_store=config_store,
    )
    logger.info(f"Mixer configured (Liquidsoap: {config.audio.liquidsoap.telnet_host}:{config.audio.liquidsoap.telnet_port})")

    # Create playlist planner (lookahead queue + library scanner)
    music_dir = project_root / config.audio.playlist.music_dir
    playlist_planner = PlaylistPlanner(
        mixer=mixer,
        db_path=db_path,
        music_dir=music_dir,
        lookahead=config.audio.playlist.lookahead,
        scan_interval=config.audio.playlist.scan_interval,
        crossfade_duration=config.audio.liquidsoap.crossfade_duration,
    )
    logger.info(f"Playlist planner configured (music_dir: {music_dir}, lookahead: {config.audio.playlist.lookahead})")

    # Create stream context (real-time "what's playing" monitor)
    stream_context = StreamContext(mixer)
    stream_context.set_planner(playlist_planner)
    logger.info("Stream context configured")

    # Create voice scheduler (central voice timing engine)
    voice_scheduler = VoiceScheduler(tts_service, mixer, stream_context)
    logger.info("Voice scheduler configured")

    # Wire event store into services for timeline instrumentation
    stream_context.set_event_store(event_store)
    voice_scheduler.set_event_store(event_store)
    tts_service.set_event_store(event_store)
    llm_service.set_event_store(event_store)
    playlist_planner.set_event_store(event_store)
    playlist_planner.set_stream_context(stream_context)

    # Shared services for plugin contexts
    ctx_kwargs = {
        "tts_service": tts_service,
        "mixer": mixer,
        "llm_service": llm_service,
        "stream_context": stream_context,
        "voice_scheduler": voice_scheduler,
        "booth": booth,
        "playlist_planner": playlist_planner,
    }

    # Load plugin instances (SQLite + YAML fallback)
    plugins = await load_plugin_instances(
        config_store=config_store,
        plugin_configs=config.plugins,
        ctx_kwargs=ctx_kwargs,
    )
    logger.info(f"Loaded {len(plugins)} plugin instance(s)")

    # Create Telegram channel
    icecast_url = f"http://localhost:{config.audio.icecast.external_port}"
    telegram = TelegramChannel(
        token=config.telegram.token,
        allowed_users=config.telegram.allowed_users,
        stream_url_getter=lambda: stream_url,
        tts_service=tts_service,
        mixer=mixer,
        stt_service=stt_service,
        llm_service=llm_service,
        station_name=station_name,
        stream_context=stream_context,
        icecast_url=icecast_url,
    )
    telegram.register_plugins(plugins)

    # Create web server
    web_server = WebServer(
        config_store=config_store,
        mixer=mixer,
        stream_context=stream_context,
        plugins=plugins,
        event_store=event_store,
        ctx_kwargs=ctx_kwargs,
        station_name=station_name,
    )

    # Set up graceful shutdown
    shutdown_event = asyncio.Event()

    # Store startup metadata and control events for system routes
    web_server.app["start_time"] = time.time()
    web_server.app["project_root"] = project_root

    def handle_shutdown(sig: signal.Signals) -> None:
        logger.info(f"Received {sig.name}, initiating shutdown...")
        shutdown_event.set()

    # Register signal handlers
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_shutdown, sig)

    # Start services
    try:
        await tts_service.start()
        await stt_service.start()
        await llm_service.start()
        await mixer.start()
        await playlist_planner.start()
        await stream_context.start()
        await voice_scheduler.start()

        # Wire feedback loop: track changes drive playlist advancement
        stream_context.on("track_changed", playlist_planner.advance)

        # Start plugins
        for plugin in plugins:
            try:
                await plugin.start()
            except Exception:
                logger.exception(f"Failed to start plugin: {plugin.instance_id}")

        await telegram.start()
        await web_server.start()

        logger.info("")
        logger.info(f"🎧 {station_name} is running!")
        logger.info(f"   Stream URL: {stream_url}")
        logger.info(f"   Web GUI:    http://{local_ip}:49995")
        logger.info(f"   Plugins:    {', '.join(p.instance_id for p in plugins) or 'none'}")
        logger.info("   Send /start to your Telegram bot to begin")
        logger.info("")
        logger.info("Press Ctrl+C to stop")

        # Wait for shutdown signal
        await shutdown_event.wait()

    except Exception as e:
        logger.exception(f"Error running {station_name}: {e}")
    finally:
        async def _cleanup() -> None:
            """Shut down all services in reverse order."""
            await web_server.stop()
            await telegram.stop()
            for plugin in reversed(plugins):
                try:
                    await plugin.stop()
                except Exception:
                    logger.exception(f"Failed to stop plugin: {plugin.instance_id}")
            await voice_scheduler.stop()
            await stream_context.stop()
            await playlist_planner.stop()
            await mixer.stop()
            await llm_service.stop()
            await stt_service.stop()
            await tts_service.stop()
            await event_store.close()
            await config_store.close()

        try:
            await asyncio.wait_for(_cleanup(), timeout=8.0)
        except asyncio.TimeoutError:
            logger.warning("Cleanup timed out after 8s — exiting anyway")
        except Exception:
            logger.exception("Error during cleanup")

        booth.stop(station_name)
        logger.info(f"{station_name} stopped.")


if __name__ == "__main__":
    asyncio.run(main())
