"""LLM backend abstraction — swap Ollama and `claude -p` per role at runtime.

Three roles the producer uses:
    - interpreter: classify a seed into a pipeline + parameters
    - script_generator: full multi-host show script
    - vision: image -> text description (Ollama-only for now)

Chat backends (ollama, claude_cli) share a common `.chat()` interface.
Vision is separate because its transport is different (base64 image payload).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import aiohttp

if TYPE_CHECKING:
    from bridge.services.llm_service import LLMService

logger = logging.getLogger(__name__)


# =========================================================================
# Chat backends
# =========================================================================


class ChatBackend(Protocol):
    name: str
    model: str

    async def chat(self, prompt: str, *, system_prompt: str) -> str:
        ...


class OllamaChatBackend:
    """Wraps the existing LLMService. Optionally overrides the model."""

    name = "ollama"

    def __init__(self, llm_service: "LLMService", model: str | None = None):
        self._llm = llm_service
        self._model_override = model

    @property
    def model(self) -> str:
        return self._model_override or self._llm.model

    async def chat(self, prompt: str, *, system_prompt: str) -> str:
        if self._model_override and self._model_override != self._llm.model:
            # Temporary per-call model swap, restore after
            original = self._llm.model
            self._llm.model = self._model_override
            try:
                return await self._llm.chat(prompt, system_prompt=system_prompt)
            finally:
                self._llm.model = original
        return await self._llm.chat(prompt, system_prompt=system_prompt)


def _resolve_claude_path() -> str:
    """Find the `claude` binary, even under systemd's minimal PATH."""
    found = shutil.which("claude")
    if found:
        return found
    for candidate in (
        Path.home() / ".local" / "bin" / "claude",
        Path("/usr/local/bin/claude"),
        Path("/opt/homebrew/bin/claude"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return "claude"  # last resort; subprocess will FileNotFoundError with a clear trace


class ClaudeCLIBackend:
    """Invoke Claude via the `claude -p` CLI subprocess.

    Uses the user's existing CLI login — no API key, no SDK.
    """

    name = "claude_cli"

    def __init__(self, model: str = "sonnet", timeout: float = 120.0):
        self.model = model
        self.timeout = timeout
        self.binary = _resolve_claude_path()

    async def chat(self, prompt: str, *, system_prompt: str) -> str:
        full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
        logger.info(f"{self.binary} -p --model {self.model} (prompt: {prompt[:60]!r}...)")
        proc = await asyncio.create_subprocess_exec(
            self.binary, "-p", "--model", self.model,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(full_prompt.encode()),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"claude CLI timed out after {self.timeout}s")
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude CLI failed (rc={proc.returncode}): {stderr.decode()[:200]}"
            )
        return stdout.decode().strip()


# =========================================================================
# Vision backend
# =========================================================================


class VisionBackend(Protocol):
    name: str
    model: str

    async def describe(self, image_path: Path, prompt: str) -> str:
        ...


class OllamaVisionBackend:
    """Describe an image via Ollama's multimodal /api/generate endpoint.

    Default model `gemma3:27b` — configurable (e.g. `llava`, `llama3.2-vision`).
    """

    name = "ollama"

    def __init__(self, base_url: str, model: str = "gemma3:27b", timeout: float = 120.0):
        # base_url ends at /v1/... — strip to get Ollama root
        if "/v1" in base_url:
            base_url = base_url.split("/v1", 1)[0]
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    async def describe(self, image_path: Path, prompt: str) -> str:
        if not image_path.exists():
            raise FileNotFoundError(f"Image not found: {image_path}")
        b64 = base64.b64encode(image_path.read_bytes()).decode()
        payload = {
            "model": self.model,
            "prompt": prompt,
            "images": [b64],
            "stream": False,
        }
        logger.info(f"ollama vision ({self.model}): {image_path.name}")
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    raise RuntimeError(
                        f"Ollama vision API error ({response.status}): {text[:200]}"
                    )
                data = await response.json()
                return (data.get("response") or "").strip()


# =========================================================================
# Factories
# =========================================================================


def build_chat_backend(cfg: dict, default_llm: "LLMService") -> ChatBackend:
    """Build a chat backend from a config dict like {backend: ollama, model: ...}.

    Unknown backends fall back to Ollama (with model override if supplied).
    """
    backend = (cfg or {}).get("backend", "ollama").lower()
    model = (cfg or {}).get("model")

    if backend == "claude_cli":
        return ClaudeCLIBackend(model=model or "sonnet")
    if backend == "ollama":
        return OllamaChatBackend(default_llm, model=model)
    logger.warning(f"Unknown chat backend {backend!r}, falling back to ollama")
    return OllamaChatBackend(default_llm, model=model)


def build_vision_backend(cfg: dict, default_llm_endpoint: str) -> VisionBackend:
    """Build a vision backend. Only Ollama supported currently."""
    backend = (cfg or {}).get("backend", "ollama").lower()
    model = (cfg or {}).get("model") or "gemma3:27b"

    if backend != "ollama":
        logger.warning(f"Vision backend {backend!r} not supported, using ollama")

    return OllamaVisionBackend(default_llm_endpoint, model=model)
