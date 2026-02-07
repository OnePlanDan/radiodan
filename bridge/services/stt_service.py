"""
RadioDan STT Service

Speech-to-Text service using Whisper API (OpenAI-compatible endpoint).
Transcribes audio files (voice messages) to text.
"""

import logging
from pathlib import Path

import aiohttp

from bridge.booth import booth

logger = logging.getLogger(__name__)


class STTService:
    """Speech-to-Text service using Whisper API."""

    def __init__(self, endpoint: str):
        """
        Initialize STT service.

        Args:
            endpoint: Whisper API endpoint (OpenAI-compatible)
                      e.g., http://localhost:5000/v1/audio/transcriptions
        """
        self.endpoint = endpoint
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        """Initialize the HTTP session."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
            logger.info(f"STT service started (endpoint: {self.endpoint})")

    async def stop(self) -> None:
        """Close the HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None
            logger.info("STT service stopped")

    async def transcribe(self, audio_path: Path) -> str:
        """
        Transcribe audio file to text.

        Args:
            audio_path: Path to audio file (OGG, WAV, MP3, etc.)

        Returns:
            Transcribed text

        Raises:
            RuntimeError: If transcription fails
        """
        if self._session is None:
            await self.start()

        booth.whisper_start()
        logger.info(f"Transcribing: {audio_path}")

        try:
            # Prepare multipart form with the audio file
            form_data = aiohttp.FormData()
            form_data.add_field(
                "file",
                audio_path.read_bytes(),
                filename=audio_path.name,
                content_type="audio/ogg",
            )
            form_data.add_field("model", "whisper-1")  # Standard OpenAI model name

            async with self._session.post(
                self.endpoint,
                data=form_data,
                timeout=aiohttp.ClientTimeout(total=60),  # Transcription can take time
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    booth.whisper_error(f"API error ({response.status})")
                    raise RuntimeError(f"STT API error ({response.status}): {error_text}")

                result = await response.json()
                text = result.get("text", "").strip()

                booth.whisper_done(text)
                logger.info(f"Transcribed: '{text[:50]}...'")
                return text

        except aiohttp.ClientError as e:
            booth.whisper_error(str(e))
            raise RuntimeError(f"STT API connection error: {e}") from e

    async def health_check(self) -> bool:
        """Check if the STT API is available."""
        if self._session is None:
            await self.start()

        try:
            # Simple connectivity check - just see if we can reach the endpoint
            # Most Whisper APIs don't have a dedicated health endpoint
            async with self._session.options(
                self.endpoint,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                # Accept various success codes
                return response.status < 500
        except Exception:
            return False
