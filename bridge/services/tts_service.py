"""
RadioDan TTS Service

Async wrapper around the Qwen3-TTS API (and any API that speaks the same
{text, speaker, instruct} -> WAV shape, e.g. Chatterbox).

Generates WAV audio files from text for streaming through Liquidsoap.

Each voice can declare a fallback chain. A single dead TTS host used to
silence the whole station — in June 2026 the local Qwen host died and Radio
Dan broadcast 41 days of unbroken music with no DJ, because the one voice in
use had nowhere else to go. Routes are now tried in order until one answers.
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from bridge.booth import booth

if TYPE_CHECKING:
    from bridge.event_store import EventStore

logger = logging.getLogger(__name__)


class TTSService:
    """Text-to-Speech service with per-voice endpoint routing and failover."""

    def __init__(
        self,
        endpoint: str,
        cache_dir: Path,
        speaker: str = "Aiden",
        language: str = "English",
        instruct: str = "Speak calmly and clearly",
        voice_map: dict[str, str] | None = None,
        fallbacks: dict | None = None,
        default_fallback: dict | None = None,
        loudness_target: float = -12.0,
        true_peak: float = -1.5,
        compress_threshold: str = "-18dB",
        compress_ratio: float = 3.0,
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
            fallbacks: Optional per-speaker failover chain, tried in order when
                the primary route fails. Each entry may override the endpoint,
                the speaker, or both — a substitute voice on a different host is
                the common case, since voice names are not portable between
                backends. Shape:
                {"Eric": [{"endpoint": "http://host/api/tts/custom",
                           "speaker": "carlin"}]}
        """
        self.endpoint = endpoint
        self.cache_dir = Path(cache_dir)
        self.speaker = speaker
        self.language = language
        self.instruct = instruct
        self.voice_map: dict[str, str] = dict(voice_map or {})
        self.fallbacks: dict[str, list[dict]] = {
            voice: list(chain or []) for voice, chain in (fallbacks or {}).items()
        }
        # Catch-all for voices with no chain of their own. Enumerating fallbacks
        # per known voice leaves any *unknown* one with nowhere to go — which is
        # how Snoop's `Adrian` (a voice the local backend does not have) silently
        # lost every one of his lines.
        self.default_fallback: dict = dict(default_fallback or {})
        self.loudness_target = loudness_target
        self.true_peak = true_peak
        self.compress_threshold = compress_threshold
        self.compress_ratio = compress_ratio
        self._session: aiohttp.ClientSession | None = None
        self._event_store: "EventStore | None" = None

        # Voice-health state, read by VoiceWatchdog and /api/status/health.
        self._started_at = time.time()
        self._last_success_at: float | None = None
        self._last_success_route: tuple[str, str] | None = None
        self._last_error: str = ""
        self._consecutive_failures = 0
        self._fallback_uses = 0

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

    # =====================================================================
    # ROUTING
    # =====================================================================

    def routes_for(self, speaker: str) -> list[tuple[str, str]]:
        """Ordered (endpoint, speaker) attempts for a voice: primary, then fallbacks.

        The primary comes from voice_map, falling back to the default endpoint.
        Duplicate routes are dropped so a misconfigured chain can't retry the
        same dead host twice.
        """
        primary = self.voice_map.get(speaker, self.endpoint)
        routes: list[tuple[str, str]] = [(primary, speaker)]
        chain = self.fallbacks.get(speaker) or ([self.default_fallback] if self.default_fallback else [])
        for entry in chain:
            if not isinstance(entry, dict):
                logger.warning(f"Ignoring malformed TTS fallback for {speaker!r}: {entry!r}")
                continue
            route = (entry.get("endpoint") or primary, entry.get("speaker") or speaker)
            if route not in routes:
                routes.append(route)
        return routes

    def known_endpoints(self) -> list[str]:
        """Every endpoint this service might call, deduplicated, primaries first."""
        seen: list[str] = [self.endpoint]
        for candidate in list(self.voice_map.values()):
            if candidate not in seen:
                seen.append(candidate)
        for chain in self.fallbacks.values():
            for entry in chain:
                if isinstance(entry, dict) and (ep := entry.get("endpoint")) and ep not in seen:
                    seen.append(ep)
        return seen

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
        effective_instruct = instruct or self.instruct
        routes = self.routes_for(effective_speaker)

        booth.tts_request(text, effective_speaker)

        eid = None
        if self._event_store:
            eid = await self._event_store.start_event(
                event_type="tts_generate", lane="system",
                title=f"TTS: {text[:30]}..." if len(text) > 30 else f"TTS: {text}",
                details={"text": text, "speaker": effective_speaker},
            )

        failures: list[str] = []
        for attempt, (target_endpoint, route_speaker) in enumerate(routes):
            is_fallback = attempt > 0
            logger.info(
                f"Generating TTS: '{text[:50]}...' "
                f"speaker={route_speaker} endpoint={target_endpoint}"
                + (f" (fallback {attempt}/{len(routes) - 1})" if is_fallback else "")
            )
            try:
                audio_data = await self._request_audio(
                    target_endpoint, text, route_speaker, effective_instruct
                )
            except Exception as e:
                failures.append(f"{target_endpoint} as {route_speaker}: {e}")
                booth.tts_error(f"{route_speaker} via {target_endpoint}: {e}")
                # Only the last failure is fatal — keep going down the chain.
                logger.warning(
                    f"TTS route failed ({route_speaker} @ {target_endpoint}): {e}"
                    + ("" if is_fallback or len(routes) > 1 else " — no fallback configured")
                )
                continue

            output_path.write_bytes(audio_data)

            # Normalize loudness (EBU R128) so all voices play at equal volume
            await self._normalize_audio(output_path)

            self._last_success_at = time.time()
            self._last_success_route = (target_endpoint, route_speaker)
            self._consecutive_failures = 0
            if is_fallback:
                self._fallback_uses += 1
                # Loud on purpose: audio is fine, but the primary voice is down
                # and someone should know before it becomes the new normal.
                logger.error(
                    f"TTS FALLBACK IN USE: {effective_speaker} is unreachable, "
                    f"served as {route_speaker} via {target_endpoint}. "
                    f"Primary failure: {failures[0] if failures else 'unknown'}"
                )
                booth.error(
                    f"TTS fallback: {effective_speaker} → {route_speaker} @ {target_endpoint}"
                )

            booth.tts_generated(str(output_path))
            logger.info(f"TTS generated: {output_path} ({len(audio_data)} bytes)")

            if self._event_store and eid is not None:
                await self._event_store.end_event(
                    eid,
                    extra_details={
                        "size_bytes": len(audio_data),
                        "path": str(output_path),
                        "endpoint": target_endpoint,
                        "route_speaker": route_speaker,
                        "fallback": "true" if is_fallback else "false",
                    },
                )
            return output_path

        # Every route failed.
        self._consecutive_failures += 1
        self._last_error = "; ".join(failures)
        if self._event_store and eid is not None:
            await self._event_store.end_event(
                eid, status="failed", extra_details={"error": self._last_error[:500]},
            )
        raise RuntimeError(
            f"TTS failed on all {len(routes)} route(s) for {effective_speaker}: "
            f"{self._last_error}"
        )

    async def _request_audio(
        self, endpoint: str, text: str, speaker: str, instruct: str
    ) -> bytes:
        """POST one TTS request and return WAV bytes. Raises on any failure.

        Non-200 responses raise too, so a sick backend and a dead one both fall
        through to the next route instead of only connection errors doing so.
        """
        payload = {"text": text, "speaker": speaker, "instruct": instruct}
        async with self._session.post(
            endpoint, json=payload, timeout=aiohttp.ClientTimeout(total=60),
        ) as response:
            if response.status != 200:
                error_text = (await response.text())[:200]
                raise RuntimeError(f"HTTP {response.status}: {error_text}")
            audio_data = await response.read()
        if not audio_data:
            raise RuntimeError("empty response body")
        return audio_data

    def _normalize_filter(self) -> str:
        """ffmpeg filter chain that brings a voice clip to the station's level.

        Measured 2026-07-29: plain `loudnorm=I=-16` produced voice at −16.9 LUFS
        against music airing at −7.6, so the DJ was ~9 dB below the songs and
        vanished at low listening volume.

        Raising the loudnorm target alone does not fix it — single-pass *and*
        two-pass both saturate near −13.4 LUFS because the true-peak ceiling
        binds first (asking for I=−10 still yielded −13.5). Compressing ahead of
        loudnorm lowers the crest factor so the requested gain actually fits:
        acompressor + I=−12 measured −13.1 LUFS at LRA 2.7, which is the honest
        ceiling for speech here without audible squashing.

        The remaining gap is closed on the Liquidsoap side by `music_vol`, since
        the real outlier is the music: −7.6 LUFS at +0.7 dBFS true peak, i.e.
        brick-walled and clipping.
        """
        return (
            f"acompressor=threshold={self.compress_threshold}:ratio={self.compress_ratio}"
            ":attack=5:release=120,"
            f"loudnorm=I={self.loudness_target}:TP={self.true_peak}:LRA=11"
        )

    async def _normalize_audio(self, path: Path) -> None:
        """Normalize audio loudness using ffmpeg EBU R128 (loudnorm)."""
        normalized = path.with_suffix(".norm.wav")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(path),
            "-af", self._normalize_filter(),
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

    # =====================================================================
    # HEALTH
    # =====================================================================

    async def probe_endpoint(self, endpoint: str) -> bool:
        """True if the TTS host answers at all — any HTTP status counts.

        Deliberately backend-agnostic: Qwen serves GET /, Chatterbox 404s it and
        keeps its health on /api/health. What we actually need to distinguish is
        "process is gone" (connection refused) from "process is up", and any HTTP
        response proves the latter. The old version probed a Qwen-specific
        /tts/speakers path built by trimming the endpoint, which resolved to
        /tts/speakers and always reported unhealthy.
        """
        if self._session is None:
            await self.start()

        parts = urlsplit(endpoint)
        root = urlunsplit((parts.scheme, parts.netloc, "/", "", ""))
        try:
            async with self._session.get(
                root, timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                _ = response.status
                return True
        except Exception:
            return False

    async def health_report(self) -> dict[str, bool]:
        """Reachability of every endpoint this service might call."""
        endpoints = self.known_endpoints()
        results = await asyncio.gather(*(self.probe_endpoint(e) for e in endpoints))
        return dict(zip(endpoints, results))

    async def health_check(self) -> bool:
        """True if the default endpoint is reachable."""
        return await self.probe_endpoint(self.endpoint)

    async def log_startup_health(self) -> dict[str, bool]:
        """Probe every endpoint at boot and say so in the log, loudly if down.

        A silent start is how the June 2026 outage went unnoticed: the service
        came up, the TTS host was gone, and nothing said a word.
        """
        report = await self.health_report()
        for endpoint, ok in report.items():
            role = "default" if endpoint == self.endpoint else "voice-mapped"
            if ok:
                logger.info(f"TTS endpoint reachable ({role}): {endpoint}")
            else:
                logger.error(f"TTS ENDPOINT UNREACHABLE ({role}): {endpoint}")
                booth.error(f"TTS endpoint unreachable: {endpoint}")
        if report and not any(report.values()):
            logger.error(
                "No TTS endpoint is reachable — the station will play music with "
                "no DJ until one comes back."
            )
        return report

    def stats(self) -> dict:
        """Voice-health snapshot for the watchdog and the API."""
        now = time.time()
        last = self._last_success_at
        return {
            "last_success_at": last,
            "seconds_since_success": round(now - last, 1) if last else None,
            "seconds_since_start": round(now - self._started_at, 1),
            "ever_succeeded": last is not None,
            "last_success_route": (
                {"endpoint": self._last_success_route[0], "speaker": self._last_success_route[1]}
                if self._last_success_route else None
            ),
            "consecutive_failures": self._consecutive_failures,
            "fallback_uses": self._fallback_uses,
            "last_error": self._last_error,
        }

    @property
    def silent_for_seconds(self) -> float:
        """How long since a voice was successfully generated.

        Measured from process start when nothing has ever succeeded, which is
        the case that matters: a restart into a dead TTS host.
        """
        baseline = self._last_success_at or self._started_at
        return time.time() - baseline
