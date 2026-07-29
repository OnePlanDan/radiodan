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
        port: int = 1235,
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
        self._pre_mute_tts_vol = 1.0
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

    def _validate_for_container(self, host_path: Path) -> tuple[bool, str]:
        """Verify a path will resolve to a real file inside the Liquidsoap container.

        Catches the silent-rid-drop class of failure: a `music_q.push` succeeds
        at the protocol level (LS returns a request id), but the request is
        immediately discarded because LS can't actually read the file.

        Three failure modes guarded:
        1. Missing/broken file or unreachable mount (resolve fails outright).
        2. Resolved target escapes every host mount root.
        3. Path traverses a symlink whose **literal absolute target string**
           is not reachable from inside the container — e.g. a `_damaged/`
           symlink that points at `/home/dln/...`. The host can resolve it
           because that's where the music actually lives, but the container
           only has `/music`, so following the link from inside the container
           fails. Filesystem `realpath` on the host hides this.

        Returns (True, "") on success or (False, reason) on failure.
        """
        try:
            resolved = host_path.resolve(strict=True)
        except FileNotFoundError:
            return (False, "file does not exist or symlink target is missing")
        except (RuntimeError, OSError) as exc:
            return (False, f"cannot resolve path: {exc}")

        if not self.path_mappings:
            return (True, "")  # No mounts known — trust caller.

        in_mounts = any(
            self._is_under(resolved, host_base) for host_base in self.path_mappings.keys()
        )
        if not in_mounts:
            return (False, f"resolved target escapes mounted roots: {resolved}")

        # Walk the symlink chain from host_path → resolved. Any absolute symlink
        # whose target is not a path the container can see (i.e. doesn't start
        # with one of our host mount roots) will be unreachable inside Liquidsoap.
        chain_ok, reason = self._symlink_chain_reachable(host_path)
        if not chain_ok:
            return (False, reason)

        return (True, "")

    @staticmethod
    def _is_under(path: Path, base: Path) -> bool:
        try:
            path.relative_to(base)
            return True
        except ValueError:
            return False

    def _symlink_chain_reachable(self, path: Path) -> tuple[bool, str]:
        """Walk a symlink chain; reject if any absolute target is unreachable
        from inside the container.

        The container only sees the **container-side** paths (the values in
        `path_mappings`, e.g. `/music`, `/tmp`). An absolute symlink target
        like `/home/dln/.../music/X.mp3` resolves correctly on the host but
        does not exist in the container's mount namespace, so Liquidsoap
        silently drops the request.

        Bounded to 40 hops to defend against pathological link cycles.
        """
        import os
        # Build the set of valid container path prefixes for absolute targets.
        container_prefixes = [Path(v) for v in self.path_mappings.values()]

        current = path
        for _ in range(40):
            if not current.is_symlink():
                return (True, "")
            target_str = os.readlink(current)
            target = Path(target_str)
            if target.is_absolute():
                # The container can only reach paths under one of its mounted
                # prefixes (e.g. /music, /tmp). Host-absolute targets like
                # /home/dln/... are unreachable even if they exist on the host.
                if not any(
                    self._is_under(target, prefix) or target == prefix
                    for prefix in container_prefixes
                ):
                    return (
                        False,
                        f"symlink target {target_str!r} is unreachable from container",
                    )
                # Map container path back to host path so we can keep walking.
                current = self._container_to_host(target)
                if current is None:
                    return (
                        False,
                        f"cannot map container path {target_str!r} back to host",
                    )
            else:
                # Relative target — interpret against the symlink's directory.
                # Relative links are reachable in the container too, since the
                # mounted directory tree is the same.
                current = (current.parent / target).resolve(strict=False)
        return (False, "symlink chain too deep")

    def _container_to_host(self, container_path: Path) -> Path | None:
        """Inverse of `_to_container_path`: map a container path back to the host."""
        for host_base, container_str in self.path_mappings.items():
            container_base = Path(container_str)
            try:
                rel = container_path.relative_to(container_base)
                return host_base / rel
            except ValueError:
                continue
        return None

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

    async def restart_liquidsoap_container(self, container_name: str) -> bool:
        """Restart the Liquidsoap Docker container — last-resort recovery.

        Used by the stuck-stream watchdog when softer recovery actions
        (skip, flush+repush) have failed. Requires the bridge user to be
        in the `docker` group.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "restart", container_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                logger.error(f"docker restart {container_name} timed out after 20s")
                return False
            if proc.returncode == 0:
                logger.warning(f"Restarted Liquidsoap container: {container_name}")
                return True
            logger.error(
                f"docker restart failed (rc={proc.returncode}): "
                f"{stderr.decode(errors='replace').strip()}"
            )
            return False
        except FileNotFoundError:
            logger.error("docker binary not found — can't restart container")
            return False
        except Exception:
            logger.exception("Failed to restart Liquidsoap container")
            return False

    # =========================================================================
    # MUSIC QUEUE (PlaylistPlanner integration)
    # =========================================================================

    async def queue_music(self, audio_path: Path, gain_db: float | None = None) -> bool:
        """Push a music track to the music_q request queue.

        Validates that the path resolves to a real file inside the Liquidsoap
        container's mounts before pushing — fails fast on broken symlinks
        rather than letting Liquidsoap silently drop the request.

        Args:
            audio_path: Path to the audio file on the host
            gain_db: Optional per-track normalisation gain, sent as a
                `replay_gain` annotation. Liquidsoap's
                `amplify(override="replay_gain")` applies it, so the file itself
                is never re-encoded and no tags are written.

        Returns:
            True if queued successfully
        """
        ok, reason = self._validate_for_container(audio_path)
        if not ok:
            booth.mixer_error(f"Music queue skipped: {reason}")
            logger.warning(f"queue_music skipped — {reason}: {audio_path}")
            return False

        async with self._lock:
            try:
                container_path = self._to_container_path(audio_path)
                uri = container_path
                if gain_db is not None:
                    # The "dB" suffix is mandatory. Liquidsoap's amplify override
                    # treats a bare float as a *linear* multiplicative factor, so
                    # "-5.00" means 5x gain with inverted phase, not -5 dB. Shipping
                    # it without the suffix put clipping, phase-inverted audio on
                    # air until it was caught.
                    uri = f'annotate:replay_gain="{gain_db:.2f} dB":{container_path}'
                response = await self._send_command(f"music_q.push {uri}")
                booth.mixer_queue("music_q", Path(container_path).name)
                gain_note = f" (gain {gain_db:+.2f} dB)" if gain_db is not None else ""
                logger.info(f"Queued music: {container_path}{gain_note} -> {response}")
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
            "tts_vol": 1.0,
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
    # MUSIC QUEUE MANAGEMENT
    # =========================================================================

    async def flush_music_queue(self) -> bool:
        """Flush all pending tracks from Liquidsoap's music_q and skip current.

        Returns:
            True if flush succeeded
        """
        async with self._lock:
            try:
                await self._send_command("music_q.flush_and_skip")
                logger.info("Flushed music_q (flush_and_skip)")
                return True
            except RuntimeError as e:
                logger.error(f"Failed to flush music queue: {e}")
                return False

    async def flush_queued_music(self) -> bool:
        """Flush pending tracks from Liquidsoap's music_q WITHOUT skipping the
        currently-playing track. Used on a seed change: current song finishes
        naturally, the new first song becomes the next to play.

        Returns:
            True if flush succeeded
        """
        async with self._lock:
            try:
                response = await self._send_command("music_q.flush_queued")
                logger.info(f"Flushed music_q pending: {response.strip()}")
                return True
            except RuntimeError as e:
                logger.error(f"Failed to flush queued music: {e}")
                return False

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
                await self._send_command("music_q.skip")
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
