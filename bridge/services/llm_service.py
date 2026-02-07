"""
RadioDan LLM Service

Provider-neutral LLM chat service using OpenAI-compatible API.
Works with Ollama, OpenAI, Anthropic, or any compatible endpoint.
"""

import logging
from typing import TYPE_CHECKING

import aiohttp

from bridge.booth import booth

if TYPE_CHECKING:
    from bridge.event_store import EventStore

logger = logging.getLogger(__name__)


class LLMService:
    """LLM chat service (provider-neutral, OpenAI-compatible API)."""

    def __init__(
        self,
        endpoint: str,
        model: str,
        system_prompt: str = "You are a friendly AI radio assistant. Keep responses concise (1-2 sentences) since they'll be spoken aloud.",
    ):
        """
        Initialize LLM service.

        Args:
            endpoint: OpenAI-compatible chat completions endpoint
                      e.g., http://localhost:11434/v1/chat/completions
            model: Model to use (e.g., "mistral", "llama3", "gpt-4o")
            system_prompt: System prompt for the assistant
        """
        self.endpoint = endpoint
        self.model = model
        self.system_prompt = system_prompt
        self._session: aiohttp.ClientSession | None = None
        self._event_store: "EventStore | None" = None

    def set_event_store(self, event_store: "EventStore") -> None:
        """Set the event store for timeline instrumentation."""
        self._event_store = event_store

    async def start(self) -> None:
        """Initialize the HTTP session."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
            logger.info(f"LLM service started (endpoint: {self.endpoint}, model: {self.model})")

    async def stop(self) -> None:
        """Close the HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None
            logger.info("LLM service stopped")

    async def chat(self, user_message: str, system_prompt: str | None = None) -> str:
        """
        Send message to LLM and return response.

        Args:
            user_message: The user's message
            system_prompt: Optional override for the default system prompt

        Returns:
            Assistant's response text

        Raises:
            RuntimeError: If chat request fails
        """
        if self._session is None:
            await self.start()

        booth.llm_request(user_message[:50] + "..." if len(user_message) > 50 else user_message)
        logger.info(f"LLM chat: '{user_message[:50]}...'")

        eid = None
        if self._event_store:
            eid = await self._event_store.start_event(
                event_type="llm_request", lane="system",
                title=f"LLM: {user_message[:30]}..." if len(user_message) > 30 else f"LLM: {user_message}",
                details={"message": user_message},
            )

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt or self.system_prompt},
                {"role": "user", "content": user_message},
            ],
        }

        try:
            async with self._session.post(
                self.endpoint,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    booth.llm_error(f"API error ({response.status})")
                    raise RuntimeError(f"LLM API error ({response.status}): {error_text}")

                result = await response.json()

                choices = result.get("choices", [])
                if not choices:
                    raise RuntimeError("LLM returned empty response")

                assistant_message = choices[0].get("message", {}).get("content", "")
                assistant_message = assistant_message.strip()

                booth.llm_response(assistant_message[:50] + "..." if len(assistant_message) > 50 else assistant_message)
                logger.info(f"LLM response: '{assistant_message[:50]}...'")

                if self._event_store and eid is not None:
                    await self._event_store.end_event(
                        eid, extra_details={"response": assistant_message[:200]},
                    )
                return assistant_message

        except aiohttp.ClientError as e:
            booth.llm_error(str(e))
            if self._event_store and eid is not None:
                await self._event_store.end_event(eid, status="failed")
            raise RuntimeError(f"LLM API connection error: {e}") from e

    async def health_check(self) -> bool:
        """Check if the LLM API is available."""
        if self._session is None:
            await self.start()

        try:
            base_url = self.endpoint.rsplit("/v1", 1)[0]
            async with self._session.get(
                f"{base_url}/api/tags",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                return response.status == 200
        except Exception:
            return False
