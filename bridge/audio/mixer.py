"""
RadioDan Liquidsoap Mixer Client

Async telnet client for controlling Liquidsoap mixing.
Queues TTS audio and earcons through request queues.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from bridge.booth import booth

if TYPE_CHECKING:
    from bridge.config_store import ConfigStore

logger = logging.getLogger(__name__)


class LiquidsoapMixer:
    """Telnet client for Liquidsoap audio mixing control."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 1234,
        path_mappings: dict[Path, str] | None = None,
        config_store: ConfigStore | None = None,
    ):
        """
        Initialize mixer client.

        Args:
            host: Liquidsoap telnet host
            port: Liquidsoap telnet port
            path_mappings: Map of host paths to container paths for path translation
                           e.g. {Path("/home/user/project/music"): "/music"}
            config_store: Optional SQLite config store for persisting volume settings
        """
        self.host = host
        self.port = port
        self.path_mappings = path_mappings or {}
        self._config_store = config_store
        self._lock = asyncio.Lock()

        # Track mute states (for toggle behavior)
        self._music_muted = False
        self._tts_muted = False
        self._earcon_muted = False
        self._pre_mute_music_vol = 1.0
        self._pre_mute_tts_vol = 0.85
        self._pre_mute_earcon_vol = 0.5

        # Track random mode state
        self._random_mode = True  # Default from station.liq playlist mode="random"

    def _to_container_path(self, host_path: Path) -> str:
        """Convert host path to container path for Liquidsoap."""
        for host_base, container_base in self.path_mappings.items():
            try:
                relative = host_path.relative_to(host_base)
                return f"{container_base}/{relative}"
            except ValueError:
                continue
        return str(host_path)

    async def _test_connection(self) -> bool:
        """Test if Liquidsoap is reachable."""
        try:
            await self._send_command("version")
            logger.info(f"Connected to Liquidsoap at {self.host}:{self.port}")
            booth.mixer_connect(self.host, self.port)
            return True
        except RuntimeError as e:
            logger.warning(f"Liquidsoap not reachable: {e}")
            return False

    async def _persist(self, key: str, value: float) -> None:
        """Save an audio setting to the database if config_store is available."""
        if self._config_store:
            await self._config_store.set("audio", key, value)

    async def _load_saved_volumes(self) -> None:
        """Load persisted volume settings from DB and apply to Liquidsoap."""
        if not self._config_store:
            return
        saved = await self._config_store.get_section("audio")
        if not saved:
            return
        for key in ("music_vol", "tts_vol", "earcon_vol", "duck_amount", "crossfade_duration",
                    "duck_in_duration", "duck_out_duration", "duck_in_curve", "duck_out_curve"):
            if key in saved:
                await self._send_command(f"var.set {key} = {float(saved[key])}")
                logger.info(f"Restored {key} = {saved[key]} from DB")
        # Update mute tracking state
        if "music_vol" in saved and float(saved["music_vol"]) > 0:
            self._pre_mute_music_vol = float(saved["music_vol"])
        if "tts_vol" in saved and float(saved["tts_vol"]) > 0:
            self._pre_mute_tts_vol = float(saved["tts_vol"])
        if "earcon_vol" in saved and float(saved["earcon_vol"]) > 0:
            self._pre_mute_earcon_vol = float(saved["earcon_vol"])

    async def _send_command(self, command: str) -> str:
        """
        Send a command to Liquidsoap and return the response.

        Opens a fresh connection for each command (Liquidsoap closes idle connections).
        """
        reader = None
        writer = None
        try:
            # Open fresh connection for this command
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=5.0,
            )

            # Send command
            writer.write(f"{command}\n".encode())
            await writer.drain()

            # Read response until "END"
            response_lines = []
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5.0)
                if not line:
                    break
                decoded = line.decode().strip()
                if decoded == "END":
                    break
                response_lines.append(decoded)

            # Send quit for clean disconnect (prevents RST race condition)
            writer.write(b"quit\n")
            await writer.drain()

            return "\n".join(response_lines)

        except (asyncio.TimeoutError, ConnectionRefusedError, OSError) as e:
            logger.error(f"Liquidsoap command failed: {e}")
            raise RuntimeError(f"Liquidsoap error: {e}") from e
        finally:
            if writer:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    async def queue_tts(self, audio_path: Path) -> bool:
        """
        Queue TTS audio file for playback.

        Args:
            audio_path: Path to the WAV audio file

        Returns:
            True if queued successfully
        """
        async with self._lock:
            try:
                container_path = self._to_container_path(audio_path)
                response = await self._send_command(f"tts.push {container_path}")
                booth.tts_queued(container_path)
                logger.info(f"Queued TTS: {container_path} -> {response}")
                return True
            except RuntimeError as e:
                booth.mixer_error(f"Queue failed: {e}")
                logger.error(f"Failed to queue TTS: {e}")
                return False

    async def queue_earcon(self, audio_path: Path) -> bool:
        """
        Queue earcon (notification sound) for playback.

        Args:
            audio_path: Path to the audio file

        Returns:
            True if queued successfully
        """
        async with self._lock:
            try:
                container_path = self._to_container_path(audio_path)
                response = await self._send_command(f"earcons.push {container_path}")
                logger.info(f"Queued earcon: {container_path} -> {response}")
                return True
            except RuntimeError as e:
                logger.error(f"Failed to queue earcon: {e}")
                return False

    async def health_check(self) -> bool:
        """Check if Liquidsoap is reachable."""
        try:
            await self._send_command("version")
            return True
        except Exception:
            return False

    # =========================================================================
    # MUSIC QUEUE (PlaylistPlanner integration)
    # =========================================================================

    async def queue_music(self, audio_path: Path) -> bool:
        """Push a music track to the music_q request queue.

        Args:
            audio_path: Path to the audio file on the host

        Returns:
            True if queued successfully
        """
        async with self._lock:
            try:
                container_path = self._to_container_path(audio_path)
                response = await self._send_command(f"music_q.push {container_path}")
                booth.mixer_queue("music_q", Path(container_path).name)
                logger.info(f"Queued music: {container_path} -> {response}")
                return True
            except RuntimeError as e:
                booth.mixer_error(f"Music queue failed: {e}")
                logger.error(f"Failed to queue music: {e}")
                return False

    async def get_music_queue_length(self) -> int:
        """Get number of tracks queued in Liquidsoap's music_q.

        Returns:
            Number of queued tracks, or 0 on error
        """
        async with self._lock:
            try:
                response = await self._send_command("music_q.queue_length")
                return int(response.strip())
            except (RuntimeError, ValueError) as e:
                logger.error(f"Failed to get music queue length: {e}")
                return 0

    async def set_crossfade_duration(self, seconds: float) -> bool:
        """Set crossfade duration in Liquidsoap.

        Args:
            seconds: Duration in seconds (clamped to 1.0–15.0)

        Returns:
            True if command succeeded
        """
        seconds = max(1.0, min(15.0, seconds))
        async with self._lock:
            try:
                await self._send_command(f"var.set crossfade_duration = {seconds}")
                await self._persist("crossfade_duration", seconds)
                logger.info(f"Set crossfade duration to {seconds}s")
                return True
            except RuntimeError as e:
                logger.error(f"Failed to set crossfade duration: {e}")
                return False

    async def get_crossfade_duration(self) -> float:
        """Read crossfade duration from Liquidsoap interactive variable.

        Returns:
            Crossfade duration in seconds, or 5.0 on error
        """
        async with self._lock:
            try:
                response = await self._send_command("var.get crossfade_duration")
                return float(response.strip())
            except (RuntimeError, ValueError) as e:
                logger.error(f"Failed to get crossfade duration: {e}")
                return 5.0

    # =========================================================================
    # VOLUME CONTROLS
    # =========================================================================

    async def set_music_volume(self, vol: float) -> bool:
        """
        Set music volume (0.0-1.0). 0 = muted/paused.

        Args:
            vol: Volume level (0.0 to 1.0)

        Returns:
            True if command succeeded
        """
        vol = max(0.0, min(1.0, vol))  # Clamp to valid range
        async with self._lock:
            try:
                await self._send_command(f"var.set music_vol = {vol}")
                self._music_muted = vol == 0.0
                if vol > 0:
                    self._pre_mute_music_vol = vol
                await self._persist("music_vol", vol)
                logger.info(f"Set music volume to {vol}")
                return True
            except RuntimeError as e:
                logger.error(f"Failed to set music volume: {e}")
                return False

    async def set_tts_volume(self, vol: float) -> bool:
        """
        Set TTS/voice volume (0.0-1.0).

        Args:
            vol: Volume level (0.0 to 1.0)

        Returns:
            True if command succeeded
        """
        vol = max(0.0, min(1.0, vol))
        async with self._lock:
            try:
                await self._send_command(f"var.set tts_vol = {vol}")
                self._tts_muted = vol == 0.0
                if vol > 0:
                    self._pre_mute_tts_vol = vol
                await self._persist("tts_vol", vol)
                logger.info(f"Set TTS volume to {vol}")
                return True
            except RuntimeError as e:
                logger.error(f"Failed to set TTS volume: {e}")
                return False

    async def set_duck_amount(self, amount: float, persist: bool = True) -> bool:
        """
        Set how much music plays during TTS (0.0-1.0).

        Args:
            amount: Duck level (0.0 = silence during TTS, 1.0 = no ducking)
            persist: If True, save to database (set False for temporary overrides)

        Returns:
            True if command succeeded
        """
        amount = max(0.0, min(1.0, amount))
        async with self._lock:
            try:
                await self._send_command(f"var.set duck_amount = {amount}")
                if persist:
                    await self._persist("duck_amount", amount)
                logger.info(f"Set duck amount to {amount}")
                return True
            except RuntimeError as e:
                logger.error(f"Failed to set duck amount: {e}")
                return False

    async def set_duck_in_duration(self, seconds: float) -> bool:
        """Set duck-in transition duration (0.05–5.0 seconds)."""
        seconds = max(0.05, min(5.0, seconds))
        async with self._lock:
            try:
                await self._send_command(f"var.set duck_in_duration = {seconds}")
                await self._persist("duck_in_duration", seconds)
                logger.info(f"Set duck-in duration to {seconds}s")
                return True
            except RuntimeError as e:
                logger.error(f"Failed to set duck-in duration: {e}")
                return False

    async def set_duck_out_duration(self, seconds: float) -> bool:
        """Set duck-out transition duration (0.05–5.0 seconds)."""
        seconds = max(0.05, min(5.0, seconds))
        async with self._lock:
            try:
                await self._send_command(f"var.set duck_out_duration = {seconds}")
                await self._persist("duck_out_duration", seconds)
                logger.info(f"Set duck-out duration to {seconds}s")
                return True
            except RuntimeError as e:
                logger.error(f"Failed to set duck-out duration: {e}")
                return False

    async def set_duck_in_curve(self, cy: float) -> bool:
        """Set duck-in bezier control point (0.0–1.0)."""
        cy = max(0.0, min(1.0, cy))
        async with self._lock:
            try:
                await self._send_command(f"var.set duck_in_curve = {cy}")
                await self._persist("duck_in_curve", cy)
                logger.info(f"Set duck-in curve to {cy}")
                return True
            except RuntimeError as e:
                logger.error(f"Failed to set duck-in curve: {e}")
                return False

    async def set_duck_out_curve(self, cy: float) -> bool:
        """Set duck-out bezier control point (0.0–1.0)."""
        cy = max(0.0, min(1.0, cy))
        async with self._lock:
            try:
                await self._send_command(f"var.set duck_out_curve = {cy}")
                await self._persist("duck_out_curve", cy)
                logger.info(f"Set duck-out curve to {cy}")
                return True
            except RuntimeError as e:
                logger.error(f"Failed to set duck-out curve: {e}")
                return False

    async def set_earcon_volume(self, vol: float) -> bool:
        """
        Set earcon/notification volume (0.0-1.0).

        Args:
            vol: Volume level (0.0 to 1.0)

        Returns:
            True if command succeeded
        """
        vol = max(0.0, min(1.0, vol))
        async with self._lock:
            try:
                await self._send_command(f"var.set earcon_vol = {vol}")
                self._earcon_muted = vol == 0.0
                if vol > 0:
                    self._pre_mute_earcon_vol = vol
                await self._persist("earcon_vol", vol)
                logger.info(f"Set earcon volume to {vol}")
                return True
            except RuntimeError as e:
                logger.error(f"Failed to set earcon volume: {e}")
                return False

    async def get_volumes(self) -> dict:
        """
        Get current volume settings.

        Returns:
            Dict with music_vol, tts_vol, earcon_vol, duck_amount (all 0.0-1.0)
        """
        result = {
            "music_vol": 1.0,
            "tts_vol": 0.85,
            "earcon_vol": 0.5,
            "duck_amount": 0.15,
            "crossfade_duration": 5.0,
            "duck_in_duration": 0.8,
            "duck_out_duration": 0.6,
            "duck_in_curve": 0.7,
            "duck_out_curve": 0.3,
        }
        async with self._lock:
            try:
                for var in ["music_vol", "tts_vol", "earcon_vol", "duck_amount", "crossfade_duration",
                            "duck_in_duration", "duck_out_duration", "duck_in_curve", "duck_out_curve"]:
                    response = await self._send_command(f"var.get {var}")
                    # Response format: "0.7" or similar
                    try:
                        result[var] = float(response.strip())
                    except ValueError:
                        logger.warning(f"Could not parse {var} value: {response}")
            except RuntimeError as e:
                logger.error(f"Failed to get volumes: {e}")
        return result

    async def toggle_music_mute(self) -> tuple[bool, float]:
        """
        Toggle music mute state.

        Returns:
            Tuple of (is_muted, current_volume)
        """
        if self._music_muted:
            # Unmute: restore previous volume
            await self.set_music_volume(self._pre_mute_music_vol)
            return (False, self._pre_mute_music_vol)
        else:
            # Mute: set to 0
            await self.set_music_volume(0.0)
            return (True, 0.0)

    async def toggle_tts_mute(self) -> tuple[bool, float]:
        """
        Toggle TTS mute state.

        Returns:
            Tuple of (is_muted, current_volume)
        """
        if self._tts_muted:
            await self.set_tts_volume(self._pre_mute_tts_vol)
            return (False, self._pre_mute_tts_vol)
        else:
            await self.set_tts_volume(0.0)
            return (True, 0.0)

    # =========================================================================
    # PLAYBACK CONTROLS
    # =========================================================================

    async def flush_tts(self) -> bool:
        """
        Flush TTS queue (clear all pending and skip current).

        Returns:
            True if command succeeded
        """
        async with self._lock:
            try:
                await self._send_command("tts.flush_and_skip")
                logger.info("Flushed TTS queue")
                return True
            except RuntimeError as e:
                logger.error(f"Failed to flush TTS: {e}")
                return False

    async def skip_tts(self) -> bool:
        """
        Skip current TTS audio.

        Returns:
            True if command succeeded
        """
        async with self._lock:
            try:
                await self._send_command("tts.skip")
                logger.info("Skipped current TTS")
                return True
            except RuntimeError as e:
                logger.error(f"Failed to skip TTS: {e}")
                return False

    async def next_track(self) -> bool:
        """
        Skip to next music track.

        Returns:
            True if command succeeded
        """
        async with self._lock:
            try:
                await self._send_command("music.skip")
                logger.info("Skipped to next track")
                return True
            except RuntimeError as e:
                logger.error(f"Failed to skip track: {e}")
                return False

    async def toggle_random(self) -> bool:
        """
        Toggle random/sequential playback mode.

        Note: Liquidsoap playlist mode is set at init, so we track this
        in memory. A full implementation would require playlist reload.

        Returns:
            New random state (True = random, False = sequential)
        """
        self._random_mode = not self._random_mode
        logger.info(f"Random mode: {'ON' if self._random_mode else 'OFF'}")
        # Note: Actual playlist mode change would require more complex handling
        # For now, we just track the state for UI display
        return self._random_mode

    @property
    def random_mode(self) -> bool:
        """Current random mode state."""
        return self._random_mode

    @property
    def music_muted(self) -> bool:
        """Current music mute state."""
        return self._music_muted

    @property
    def tts_muted(self) -> bool:
        """Current TTS mute state."""
        return self._tts_muted

    # =========================================================================
    # TRACK METADATA QUERIES
    # =========================================================================

    async def get_track_info(self) -> dict:
        """
        Query current track metadata from Liquidsoap.

        Returns:
            Dict with keys: artist, title, filename, genre, year, album
        """
        info = {
            "artist": "",
            "title": "",
            "filename": "",
            "genre": "",
            "year": "",
            "album": "",
        }
        async with self._lock:
            try:
                response = await self._send_command("music.info")
                for line in response.strip().split("\n"):
                    if "=" in line:
                        key, _, value = line.partition("=")
                        key = key.strip()
                        if key in info:
                            info[key] = value.strip()
            except RuntimeError as e:
                logger.error(f"Failed to get track info: {e}")
        return info

    async def get_remaining(self) -> float:
        """
        Query seconds remaining in current track.

        Returns:
            Seconds remaining, or -1.0 on error
        """
        async with self._lock:
            try:
                response = await self._send_command("music.remaining")
                return float(response.strip())
            except (RuntimeError, ValueError) as e:
                logger.error(f"Failed to get remaining time: {e}")
                return -1.0

    async def get_elapsed(self) -> float:
        """
        Query seconds elapsed in current track.

        Returns:
            Seconds elapsed, or -1.0 on error
        """
        async with self._lock:
            try:
                response = await self._send_command("music.elapsed")
                return float(response.strip())
            except (RuntimeError, ValueError) as e:
                logger.error(f"Failed to get elapsed time: {e}")
                return -1.0

    async def start(self) -> None:
        """Start the mixer (test connection, restore saved volumes)."""
        connected = await self._test_connection()
        if connected:
            try:
                await self._load_saved_volumes()
            except Exception:
                logger.exception("Failed to load saved volumes")

    async def stop(self) -> None:
        """Stop the mixer (no-op, connections are per-command)."""
        pass
