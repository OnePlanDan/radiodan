"""
AudioSegment client — commissioning programme episodes.

AudioSegment is a separate service that produces finished, mastered audio from a
brief. The station commissions an episode against a show, waits, and receives a
file. In broadcast terms this is an outside production house delivering to spec.

Two facts from its own history (591 episodes, 89 hours, Feb–Jul 2026) shape how
this client is used:

  build_minutes ≈ 13.0 + 1.04 × length_minutes

- **It is slow.** ~20 minutes to build a 7-minute episode, p90 of 34 minutes for
  that size. Nothing here can be requested at air time; commissions need 45–60
  minutes of lead. (The service's own guide says "3–6 minutes" — the measured
  history says otherwise, so the station plans against the history.)
- **Short is expensive.** The ~13 minute fixed overhead means a 2-minute episode
  costs 7.5× realtime while a 25-minute one costs 1.6×. Commission long blocks,
  not short segments.

The station's contract with it: an episode is a block to be scheduled, like a
song. Same queue, different material.
"""

import asyncio
import logging
from pathlib import Path

import aiohttp

logger = logging.getLogger(__name__)

# The service's guide calls this host `mnemosyne`; forge has no DNS entry for it,
# so the wg address is the working default. Override via audiosegment.base_url.
DEFAULT_BASE_URL = "http://10.10.0.9:8100/api"

# Reported by GET /api/jobs/{id}. The first six are all "still working".
PENDING_STATUSES = frozenset({
    "queued", "researching", "scripting", "scripted", "synthesizing", "mastering",
})
DONE = "completed"
FAILED = "failed"


class AudioSegmentError(RuntimeError):
    """The service was reachable but refused or could not answer."""


class AudioSegmentClient:
    """Thin async client. Knows the protocol; holds no scheduling policy."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        download_timeout: float = 300.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.download_timeout = download_timeout
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession()

    async def stop(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        if self._session is None:
            await self.start()
        url = f"{self.base_url}{path}"
        try:
            async with self._session.request(
                method, url, timeout=aiohttp.ClientTimeout(total=self.timeout), **kwargs
            ) as response:
                body = await response.text()
                if response.status >= 400:
                    raise AudioSegmentError(f"{method} {path} → {response.status}: {body[:200]}")
                import json as _json
                return _json.loads(body) if body else {}
        except aiohttp.ClientError as e:
            raise AudioSegmentError(f"{method} {path} failed: {e}") from e
        except asyncio.TimeoutError as e:
            raise AudioSegmentError(f"{method} {path} timed out after {self.timeout}s") from e

    # =====================================================================
    # DISCOVERY
    # =====================================================================

    async def health(self) -> dict:
        """Service health. Worth checking before committing to a commission."""
        return await self._request("GET", "/health")

    async def is_healthy(self) -> bool:
        try:
            return (await self.health()).get("status") == "healthy"
        except AudioSegmentError:
            return False

    async def shows(self) -> list[dict]:
        """Available shows. A commission names one of these."""
        payload = await self._request("GET", "/shows")
        if isinstance(payload, list):
            return payload
        return payload.get("shows", [])

    async def show_status(self, show: str) -> dict:
        """Episode count, next episode number, and the show's current rhythm."""
        return await self._request("GET", f"/shows/{show}/status")

    # =====================================================================
    # COMMISSIONING
    # =====================================================================

    async def produce(
        self,
        show: str,
        concept: str,
        location: str | None = None,
        weight: int | None = None,
        context_mode: str | None = None,
    ) -> dict:
        """Commission an episode. Returns immediately with a job_id.

        `concept` is a sentence or two — the brief. The show's assembler adds
        everything else: bible, recap of prior episodes, traits, open debts, and
        weather for `location`. Deliberately not passing a full script: the
        production side owns how an episode is written.
        """
        body: dict = {"concept": concept}
        if location:
            body["location"] = location
        if weight is not None:
            body["weight"] = weight
        if context_mode:
            body["context_mode"] = context_mode

        result = await self._request("POST", f"/shows/{show}/produce", json=body)
        job_id = result.get("job_id")
        if not job_id:
            raise AudioSegmentError(f"produce returned no job_id: {result}")
        logger.info(
            f"Commissioned '{concept[:60]}' from {show} "
            f"→ job {job_id} (queue position {result.get('position', '?')})"
        )
        return result

    async def job(self, job_id: str) -> dict:
        """Current state of a commission."""
        return await self._request("GET", f"/jobs/{job_id}")

    async def requeue(self, job_id: str) -> dict:
        """Retry a failed job. Resumes at `scripted` if a script already exists,
        so a retry does not repeat the expensive scriptwriting."""
        return await self._request("POST", f"/jobs/{job_id}/requeue")

    async def feedback(self, job_id: str, rating: int, comment: str = "") -> dict:
        """Rate a delivered episode; the score feeds future productions."""
        return await self._request(
            "POST", f"/jobs/{job_id}/feedback", json={"rating": rating, "comment": comment}
        )

    async def download_audio(self, job_id: str, dest: Path) -> Path:
        """Fetch the mastered audio. 409 until the job is completed.

        Streams to a temporary name and renames on success, so a partial download
        can never be mistaken for a deliverable by whatever scans the directory.
        """
        if self._session is None:
            await self.start()

        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_suffix(dest.suffix + ".part")

        url = f"{self.base_url}/jobs/{job_id}/audio"
        try:
            async with self._session.get(
                url, timeout=aiohttp.ClientTimeout(total=self.download_timeout)
            ) as response:
                if response.status == 409:
                    raise AudioSegmentError(f"job {job_id} is not finished yet")
                if response.status >= 400:
                    raise AudioSegmentError(f"download of {job_id} → {response.status}")
                with partial.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        handle.write(chunk)
        except aiohttp.ClientError as e:
            partial.unlink(missing_ok=True)
            raise AudioSegmentError(f"download of {job_id} failed: {e}") from e
        except asyncio.TimeoutError as e:
            partial.unlink(missing_ok=True)
            raise AudioSegmentError(f"download of {job_id} timed out") from e

        if partial.stat().st_size == 0:
            partial.unlink(missing_ok=True)
            raise AudioSegmentError(f"download of {job_id} was empty")

        partial.replace(dest)
        logger.info(f"Downloaded episode {job_id}: {dest.name} ({dest.stat().st_size} bytes)")
        return dest


def is_pending(status: str | None) -> bool:
    return status in PENDING_STATUSES


def is_terminal(status: str | None) -> bool:
    return status in (DONE, FAILED)


def estimated_build_minutes(length_minutes: float) -> float:
    """Fit from 591 real episodes: 13 min fixed overhead, then ~1x realtime.

    Used for lead time, so it deliberately reflects measured history rather than
    the service's own optimistic estimate.
    """
    return 13.0 + 1.04 * max(0.0, length_minutes)
