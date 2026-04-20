"""Script executor — TTS materialization + voice scheduling."""

from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path
from typing import TYPE_CHECKING

from bridge.audio.voice_scheduler import VoiceSegment
from bridge.plugins.producer.models import CharacterConfig, Script, ScriptSegment

if TYPE_CHECKING:
    from bridge.plugins.base import PluginContext

logger = logging.getLogger(__name__)


class ScriptExecutor:
    """Maps Script segments into the execution pipeline."""

    def __init__(self, ctx: "PluginContext", characters: dict[str, CharacterConfig]):
        self.ctx = ctx
        self.characters = characters

    async def materialize_segment(self, segment: ScriptSegment) -> None:
        """Pre-generate TTS for all voice cues in a segment."""
        segment.state = "materializing"
        for cue in segment.voice_cues:
            if not cue.text:
                continue
            char = self.characters.get(cue.character_id)
            if not char:
                logger.warning(f"Unknown character '{cue.character_id}' in voice cue, skipping")
                continue
            try:
                audio_path = await self.ctx.tts_service.speak(
                    cue.text,
                    speaker=char.voice_speaker,
                    instruct=char.voice_instruct,
                )
                cue.tts_audio = audio_path
                cue.tts_duration = await _get_audio_duration(audio_path)
            except Exception:
                logger.exception(f"TTS failed for cue: {cue.text[:50]}...")
        segment.state = "ready"

    async def materialize_ahead(self, script: Script, count: int = 4) -> None:
        """Pre-generate TTS for the next N unmaterialized segments."""
        to_do = [s for s in script.remaining if s.state == "planned"][:count]
        for segment in to_do:
            try:
                await self.materialize_segment(segment)
            except Exception:
                logger.exception(f"Failed to materialize segment {segment.position}")
                segment.state = "planned"  # Will retry later

    async def schedule_voice(self, segment: ScriptSegment) -> None:
        """Submit all voice cues for a segment to the voice scheduler."""
        for i, cue in enumerate(segment.voice_cues):
            if not cue.text:
                continue

            char = self.characters.get(cue.character_id)
            trigger = _style_to_trigger(cue.style)

            voice_seg = VoiceSegment(
                text=cue.text,
                trigger=trigger,
                priority=50 + i,
                pre_generated_audio=cue.tts_audio,
                audio_duration=cue.tts_duration,
                speaker=char.voice_speaker if char else None,
                instruct=char.voice_instruct if char else None,
                source_plugin="producer",
                leading_silence=0.5 if i == 0 else 0.2,
                trailing_silence=0.3,
            )
            await self.ctx.voice_scheduler.submit(voice_seg)


def _style_to_trigger(style: str) -> str:
    """Map voice cue style to VoiceScheduler trigger."""
    trigger_map = {
        "intro": "asap",
        "outro": "before_end:25",
        "mid_song": f"after_start:{random.randint(30, 120)}",
        "between_songs": "between_songs",
        "bridge": "bridge",
    }
    return trigger_map.get(style, "asap")


async def _get_audio_duration(path: Path) -> float:
    """Get audio duration in seconds via ffprobe."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        return float(stdout.decode().strip())
    except Exception:
        logger.warning(f"Could not get duration for {path}, defaulting to 5s")
        return 5.0
