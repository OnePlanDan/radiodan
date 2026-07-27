"""
RadioDan TTS Service

Async wrapper around the Qwen3-TTS API.
Generates WAV audio files from text for streaming through Liquidsoap.
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp

from bridge.booth import booth

if TYPE_CHECKING:
    from bridge.event_store import EventStore

logger = logging.getLogger(__name__)


class TTSService:
    """Text-to-Speech service using Qwen3-TTS API."""

    def __init__(
        self,
        endpoint: str,
        cache_dir: Path,
        speaker: str = "Aiden",
        language: str = "English",
        instruct: str = "Speak calmly and clearly",
        voice_map: dict[str, str] | None = None,
    ):
        """
        Initialize TTS service.

        Args:
            endpoint: Default TTS API endpoint
            cache_dir: Directory to save generated audio files
            speaker: Default voice
            language: Language for TTS
            instruct: Default voice style instruction
            voice_map: Optional per-speaker endpoint override — routes specific
                voices to alternate TTS services. e.g. {"laniv3": "http://..."}
        """
        self.endpoint = endpoint
        self.cache_dir = Path(cache_dir)
        self.speaker = speaker
        self.language = language
        self.instruct = instruct
        self.voice_map: dict[str, str] = dict(voice_map or {})
        self._session: aiohttp.ClientSession | None = None
        self._event_store: "EventStore | None" = None

    def set_event_store(self, event_store: "EventStore") -> None:
        """Set the event store for timeline instrumentation."""
        self._event_store = event_store

    async def start(self) -> None:
        """Initialize the HTTP session."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
            logger.info(f"TTS service started (endpoint: {self.endpoint})")

    async def stop(self) -> None:
        """Close the HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None
            logger.info("TTS service stopped")

    async def speak(
        self,
        text: str,
        speaker: str | None = None,
        instruct: str | None = None,
    ) -> Path:
        """
        Generate TTS audio from text.

        Args:
            text: Text to convert to speech
            speaker: Override default speaker
            instruct: Override default voice instruction

        Returns:
            Path to the generated WAV file

        Raises:
            RuntimeError: If TTS generation fails
        """
        if self._session is None:
            await self.start()

        # Generate unique filename
        timestamp = int(time.time() * 1000)
        output_path = self.cache_dir / f"msg_{timestamp}.wav"

        effective_speaker = speaker or self.speaker
        target_endpoint = self.voice_map.get(effective_speaker, self.endpoint)

        # Prepare JSON payload for the API
        payload = {
            "text": text,
            "speaker": effective_speaker,
            "instruct": instruct or self.instruct,
        }

        booth.tts_request(text, effective_speaker)
        logger.info(
            f"Generating TTS: '{text[:50]}...' "
            f"speaker={effective_speaker} endpoint={target_endpoint}"
        )

        eid = None
        if self._event_store:
            eid = await self._event_store.start_event(
                event_type="tts_generate", lane="system",
                title=f"TTS: {text[:30]}..." if len(text) > 30 else f"TTS: {text}",
                details={"text": text, "speaker": speaker or self.speaker},
            )

        try:
            async with self._session.post(
                target_endpoint, json=payload,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    booth.tts_error(f"API error ({response.status})")
                    if self._event_store and eid is not None:
                        await self._event_store.end_event(eid, status="failed")
                    raise RuntimeError(f"TTS API error ({response.status}): {error_text}")

                # Save the WAV file
                audio_data = await response.read()
                output_path.write_bytes(audio_data)

                # Normalize loudness (EBU R128) so all voices play at equal volume
                await self._normalize_audio(output_path)

                booth.tts_generated(str(output_path))
                logger.info(f"TTS generated: {output_path} ({len(audio_data)} bytes)")

                if self._event_store and eid is not None:
                    await self._event_store.end_event(
                        eid, extra_details={"size_bytes": len(audio_data), "path": str(output_path)},
                    )
                return output_path

        except aiohttp.ClientError as e:
            booth.tts_error(str(e))
            if self._event_store and eid is not None:
                await self._event_store.end_event(eid, status="failed")
            raise RuntimeError(f"TTS API connection error: {e}") from e

    async def _normalize_audio(self, path: Path) -> None:
        """Normalize audio loudness using ffmpeg EBU R128 (loudnorm)."""
        normalized = path.with_suffix(".norm.wav")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(path),
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            str(normalized),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning("Audio normalization timed out after 30s, using original")
            normalized.unlink(missing_ok=True)
            return
        if proc.returncode == 0 and normalized.exists():
            normalized.replace(path)
        else:
            logger.warning(f"Audio normalization failed (rc={proc.returncode}), using original")
            normalized.unlink(missing_ok=True)

    async def cleanup_cache(self, max_age_hours: float = 24) -> int:
        """Delete TTS cache files older than max_age_hours. Returns count deleted."""
        cutoff = time.time() - (max_age_hours * 3600)
        deleted = 0
        for f in self.cache_dir.glob("msg_*.wav"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    deleted += 1
            except OSError:
                pass
        if deleted:
            logger.info(f"TTS cache cleanup: deleted {deleted} files older than {max_age_hours}h")
        return deleted

    async def health_check(self) -> bool:
        """Check if the TTS API is available."""
        if self._session is None:
            await self.start()

        try:
            # Try to reach the speakers endpoint as a health check
            base_url = self.endpoint.rsplit("/", 1)[0]
            async with self._session.get(f"{base_url}/speakers", timeout=aiohttp.ClientTimeout(total=5)) as response:
                return response.status == 200
        except Exception:
            return False
