"""
RadioDan Ollama Service

Ollama LLM chat service using OpenAI-compatible API.
Provides conversational AI responses for voice chat mode.
"""

import logging

import aiohttp

from bridge.booth import booth

logger = logging.getLogger(__name__)


class OllamaService:
    """Ollama LLM chat service."""

    def __init__(
        self,
        endpoint: str,
        model: str,
        system_prompt: str = "You are a friendly AI radio assistant. Keep responses concise (1-2 sentences) since they'll be spoken aloud.",
    ):
        """
        Initialize Ollama service.

        Args:
            endpoint: Ollama API endpoint (OpenAI-compatible)
                      e.g., http://localhost:11434/v1/chat/completions
            model: Model to use (e.g., "mistral", "llama3")
            system_prompt: System prompt for the assistant
        """
        self.endpoint = endpoint
        self.model = model
        self.system_prompt = system_prompt
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        """Initialize the HTTP session."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
            logger.info(f"Ollama service started (endpoint: {self.endpoint}, model: {self.model})")

    async def stop(self) -> None:
        """Close the HTTP session."""
        if self._session:
            await self._session.close()
            self._session = None
            logger.info("Ollama service stopped")

    async def chat(self, user_message: str, system_prompt: str | None = None) -> str:
        """
        Send message to Ollama and return response.

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

        booth.ollama_request(user_message[:50] + "..." if len(user_message) > 50 else user_message)
        logger.info(f"Ollama chat: '{user_message[:50]}...'")

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
                timeout=aiohttp.ClientTimeout(total=120),  # LLM responses can be slow
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    booth.ollama_error(f"API error ({response.status})")
                    raise RuntimeError(f"Ollama API error ({response.status}): {error_text}")

                result = await response.json()

                # Extract assistant message from OpenAI-compatible response
                choices = result.get("choices", [])
                if not choices:
                    raise RuntimeError("Ollama returned empty response")

                assistant_message = choices[0].get("message", {}).get("content", "")
                assistant_message = assistant_message.strip()

                booth.ollama_response(assistant_message[:50] + "..." if len(assistant_message) > 50 else assistant_message)
                logger.info(f"Ollama response: '{assistant_message[:50]}...'")
                return assistant_message

        except aiohttp.ClientError as e:
            booth.ollama_error(str(e))
            raise RuntimeError(f"Ollama API connection error: {e}") from e

    async def health_check(self) -> bool:
        """Check if the Ollama API is available."""
        if self._session is None:
            await self.start()

        try:
            # Check Ollama health via models endpoint
            base_url = self.endpoint.rsplit("/v1", 1)[0]
            async with self._session.get(
                f"{base_url}/api/tags",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                return response.status == 200
        except Exception:
            return False
