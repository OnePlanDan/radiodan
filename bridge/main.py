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
from bridge.services.voice_watchdog import VoiceWatchdog
from bridge.audio.mixer import LiquidsoapMixer
from bridge.audio.stream_context import StreamContext
from bridge.audio.voice_scheduler import VoiceScheduler
from bridge.audio.playlist_planner import PlaylistPlanner
from bridge.audio.loudness import LoudnessScanner
from bridge.audio.listener_tracker import ListenerTracker
from bridge.services.audiosegment import AudioSegmentClient
from bridge.services.commissions import CommissionService
from bridge.services.greeter import GreeterService
from bridge.services.station_stats import StationStats
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

    # Determine station directory (required — no silent fallback)
    station_dir_env = os.environ.get("RADIODAN_STATION_DIR")
    if not station_dir_env:
        logger.critical(
            "RADIODAN_STATION_DIR not set. "
            "Run via ./run_radiodan.sh or export RADIODAN_STATION_DIR=<path>"
        )
        raise SystemExit(1)
    station_dir = Path(station_dir_env)
    if not station_dir.is_dir():
        logger.critical(f"Station directory does not exist: {station_dir}")
        raise SystemExit(1)

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

    # Check Telegram configuration
    telegram_enabled = config.telegram.enabled and bool(config.telegram.token)
    if not telegram_enabled:
        if not config.telegram.enabled:
            logger.info("Telegram channel disabled in station config")
        else:
            logger.info("Telegram channel disabled (no TELEGRAM_BOT_TOKEN set)")
    else:
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
        voice_map=config.audio.tts.voice_map,
        fallbacks=config.audio.tts.fallbacks,
        default_fallback=config.audio.tts.default_fallback,
        loudness_target=config.audio.tts.loudness_target,
        true_peak=config.audio.tts.true_peak,
        compress_threshold=config.audio.tts.compress_threshold,
        compress_ratio=config.audio.tts.compress_ratio,
    )
    logger.info(f"TTS service configured (endpoint: {config.audio.tts.endpoint})")
    if config.audio.tts.fallbacks:
        for voice, chain in config.audio.tts.fallbacks.items():
            routes = " → ".join(
                f"{e.get('speaker', voice)}@{e.get('endpoint', 'primary')}"
                for e in chain if isinstance(e, dict)
            )
            logger.info(f"TTS fallback for {voice}: {routes}")
    else:
        logger.warning(
            "No TTS fallbacks configured — a single dead TTS host will silence the DJ"
        )

    # Alerts when voice segments stop reaching air even though music keeps playing.
    voice_watchdog = VoiceWatchdog(
        tts_service=tts_service,
        alert_after_seconds=config.audio.tts.silence_alert_hours * 3600,
        check_interval=config.audio.tts.silence_check_interval,
    )

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
        normalization=config.audio.normalization,
    )
    # Created after the planner opens its DB (it needs that connection).
    loudness_scanner: LoudnessScanner | None = None

    # Presence history cannot be backfilled, so this samples from boot regardless
    # of whether anything consumes it yet.
    # Commissioning: order episodes from AudioSegment, collect them when they
    # land, offer them to the queue like any other block of material.
    segment_client: AudioSegmentClient | None = None
    commissions: CommissionService | None = None
    if config.audio.audiosegment.enabled:
        seg = config.audio.audiosegment
        segment_client = AudioSegmentClient(base_url=seg.base_url)
        commissions = CommissionService(
            client=segment_client,
            db_path=db_path,
            programme_dir=music_dir / seg.programme_dir,
            poll_interval=seg.poll_interval,
            auto_requeue=seg.auto_requeue,
            owned_shows=seg.owned_shows,
        )

    listener_tracker = ListenerTracker(
        db_path=db_path,
        status_url=f"http://localhost:{config.audio.icecast.external_port}/status-json.xsl",
    )
    logger.info(f"Playlist planner configured (music_dir: {music_dir}, lookahead: {config.audio.playlist.lookahead})")

    # Station statistics — read-only reporter used by /api/stats and greetings.
    station_stats = StationStats(db_path=db_path, music_dir=music_dir)

    # Resolve watchdog fallback track path (absolute or relative to music_dir)
    fallback_path: Path | None = None
    raw_fallback = config.audio.watchdog.fallback_track_path
    if raw_fallback:
        candidate = Path(raw_fallback)
        if not candidate.is_absolute():
            candidate = music_dir / candidate
        if candidate.exists():
            fallback_path = candidate
            logger.info(f"Watchdog fallback track: {fallback_path}")
        else:
            logger.warning(
                f"Watchdog fallback track configured but missing on disk: {candidate}"
            )

    # Create stream context (real-time "what's playing" monitor)
    stream_context = StreamContext(
        mixer,
        grace_seconds=config.audio.watchdog.grace_seconds,
        min_track_duration=config.audio.watchdog.min_track_duration,
        liquidsoap_container_name=config.audio.watchdog.liquidsoap_container_name,
        fallback_track_path=fallback_path,
    )
    stream_context.set_planner(playlist_planner)
    logger.info(
        f"Stream context configured (watchdog grace={config.audio.watchdog.grace_seconds}s, "
        f"container={config.audio.watchdog.liquidsoap_container_name})"
    )

    # Create voice scheduler (central voice timing engine)
    voice_scheduler = VoiceScheduler(tts_service, mixer, stream_context)
    logger.info("Voice scheduler configured")

    # Greeter — notices a listener connecting, greets them, and runs the daily
    # bulletin (ordered every day whether anyone listens; aired on the day's
    # first connection).
    greeter = GreeterService(
        tracker=listener_tracker,
        tts_service=tts_service,
        mixer=mixer,
        voice_scheduler=voice_scheduler,
        planner=playlist_planner,
        stream_context=stream_context,
        db_path=db_path,
        commissions=commissions,
        stats=station_stats,
        enabled=config.greeter.enabled,
        listener_name=config.greeter.listener_name,
        poll_interval=config.greeter.poll_interval,
        cooldown_seconds=config.greeter.cooldown_seconds,
        boot_grace_seconds=config.greeter.boot_grace_seconds,
        speaker=config.greeter.speaker,
        instruct=config.greeter.instruct,
        news_show=config.greeter.news_show,
        news_hour=config.greeter.news_hour,
        first_connect_episode=config.greeter.first_connect_episode,
        location=config.audio.audiosegment.location,
    )

    # Wire event store into services for timeline instrumentation
    stream_context.set_event_store(event_store)
    voice_scheduler.set_event_store(event_store)
    tts_service.set_event_store(event_store)
    llm_service.set_event_store(event_store)
    playlist_planner.set_event_store(event_store)
    playlist_planner.set_stream_context(stream_context)
    voice_watchdog.set_event_store(event_store)

    # Shared services for plugin contexts
    ctx_kwargs = {
        "tts_service": tts_service,
        "stt_service": stt_service,
        "mixer": mixer,
        "llm_service": llm_service,
        "stream_context": stream_context,
        "voice_scheduler": voice_scheduler,
        "booth": booth,
        "playlist_planner": playlist_planner,
        "known_good_fallback_path": fallback_path,
        "config_store": config_store,
    }

    # Load plugin instances (SQLite + YAML fallback)
    plugins = await load_plugin_instances(
        config_store=config_store,
        plugin_configs=config.plugins,
        ctx_kwargs=ctx_kwargs,
    )
    logger.info(f"Loaded {len(plugins)} plugin instance(s)")

    # Create Telegram channel (optional)
    telegram = None
    if telegram_enabled:
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

    # Create API server
    web_server = WebServer(
        config_store=config_store,
        mixer=mixer,
        stream_context=stream_context,
        plugins=plugins,
        event_store=event_store,
        ctx_kwargs=ctx_kwargs,
        station_name=station_name,
        stream_url=stream_url,
        project_root=project_root,
        voice_watchdog=voice_watchdog,
        listener_tracker=listener_tracker,
        commissions=commissions,
        greeter=greeter,
        station_stats=station_stats,
    )

    # Set up graceful shutdown
    shutdown_event = asyncio.Event()

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
        if config.audio.tts.health_check_on_start:
            await tts_service.log_startup_health()
        await stt_service.start()
        await llm_service.start()
        await mixer.start()
        await playlist_planner.start()
        if config.audio.normalization.enabled and playlist_planner._db is not None:
            norm = config.audio.normalization
            loudness_scanner = LoudnessScanner(
                playlist_planner._db,
                concurrency=norm.scan_concurrency,
                batch_size=norm.scan_batch_size,
                pause_between_batches=norm.scan_pause_seconds,
            )
            pending = await loudness_scanner.pending_count()
            logger.info(
                f"Music normalisation on (target {norm.target_lufs} LUFS); "
                f"{pending} track(s) awaiting measurement"
            )
            await loudness_scanner.start()
        await stream_context.start()
        await voice_scheduler.start()
        await voice_watchdog.start()
        await listener_tracker.start()
        await station_stats.start()
        if commissions is not None:
            await segment_client.start()
            await commissions.start()
            if not await segment_client.is_healthy():
                logger.warning(
                    f"AudioSegment unreachable at {config.audio.audiosegment.base_url} — "
                    "commissions will retry; music is unaffected"
                )
        # After commissions: the greeter orders and airs the daily bulletin.
        await greeter.start()

        # Replay knobs turned on /settings over the YAML config — a setting
        # changed through the GUI survives restarts without editing YAML.
        try:
            saved = await config_store.get_section("greeter")
            if saved:
                applied = greeter.apply_settings(saved)
                logger.info(f"Restored greeter settings from store: {applied}")
            if commissions is not None:
                saved = await config_store.get_section("commissions")
                if "max_per_day" in saved:
                    commissions.max_per_day = max(1, int(saved["max_per_day"]))
                if "auto_requeue" in saved:
                    commissions.auto_requeue = bool(saved["auto_requeue"])
        except Exception:
            logger.exception("Could not replay stored settings (using YAML values)")

        # Wire feedback loop: track changes drive playlist advancement
        stream_context.on("track_changed", playlist_planner.advance)

        # Start plugins (producer last — it replaces the playlist feeder)
        non_producers = [p for p in plugins if p.name != "producer"]
        producers = [p for p in plugins if p.name == "producer"]
        for plugin in non_producers + producers:
            try:
                await plugin.start()
            except Exception:
                logger.exception(f"Failed to start plugin: {plugin.instance_id}")

        # Producer owns talk — silence presenters to prevent double-talking
        if any(p.name == "producer" and p._running for p in plugins):
            for p in plugins:
                if p.name == "presenter" and hasattr(p, "_active"):
                    p._active = False
                    logger.info(f"Silenced presenter (producer active): {p.instance_id}")

        if telegram:
            await telegram.start()
        await web_server.start()

        logger.info("")
        logger.info(f"🎧 {station_name} is running!")
        logger.info(f"   Stream URL: {stream_url}")
        logger.info(f"   API:        http://{local_ip}:49997")
        logger.info(f"   Plugins:    {', '.join(p.instance_id for p in plugins) or 'none'}")
        if telegram:
            logger.info("   Send /start to your Telegram bot to begin")
        logger.info("")
        logger.info("Press Ctrl+C to stop")

        # Periodic housekeeping (TTS cache only — event history is kept)
        async def _periodic_cleanup():
            while True:
                await asyncio.sleep(3600)  # Every hour
                try:
                    await tts_service.cleanup_cache(max_age_hours=24)
                except Exception:
                    logger.exception("TTS cache cleanup failed")

        cleanup_task = asyncio.create_task(_periodic_cleanup())

        # Wait for shutdown signal
        await shutdown_event.wait()

    except Exception as e:
        logger.exception(f"Error running {station_name}: {e}")
    finally:
        cleanup_task.cancel()

        async def _cleanup() -> None:
            """Shut down all services in reverse order."""
            await web_server.stop()
            if telegram:
                await telegram.stop()
            for plugin in reversed(plugins):
                try:
                    await plugin.stop()
                except Exception:
                    logger.exception(f"Failed to stop plugin: {plugin.instance_id}")
            await voice_watchdog.stop()
            await greeter.stop()
            await listener_tracker.stop()
            await station_stats.stop()
            if commissions is not None:
                await commissions.stop()
                await segment_client.stop()
            if loudness_scanner is not None:
                await loudness_scanner.stop()
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
