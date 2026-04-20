"""Script generator — build prompt, call LLM, parse structured JSON response."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import TYPE_CHECKING

from bridge.plugins.producer.models import (
    MAX_EXCHANGES_PER_SEGMENT,
    CharacterConfig,
    Script,
    ScriptSegment,
    SeedState,
    VoiceCue,
)

if TYPE_CHECKING:
    from bridge.services.llm_backends import ChatBackend

logger = logging.getLogger(__name__)

SCRIPT_SYSTEM_PROMPT = f"""\
You are a radio show producer. Given a cast, a song list, and context, \
produce a JSON script for a radio segment. Each segment pairs a song with optional DJ talk.

Rules:
- Not every song needs talk. Silent segments (empty voice array) create breathing room.
- Aim for talk on about 60-70% of songs — leave some silent.
- Vary talk styles: intro (as song starts), outro (near song end), mid_song (mid-track comment), between_songs (in the gap).
- When more than one host is listed, they can share a segment as short back-and-forth dialogue. \
Alternate speakers — ABAB, ABABA, or BABA patterns are natural. Solo A or solo B segments are also fine. \
Cap any one segment at {MAX_EXCHANGES_PER_SEGMENT} voice lines total.
- Keep each voice line to 1-3 sentences. These will be spoken aloud via TTS.
- Stay in character. Reference the seed, mood, and context naturally where it fits — don't force it.
- No hashtags, emojis, or markdown in talk text.

Respond with ONLY valid JSON:
{{
  "segments": [
    {{
      "song_index": 0,
      "voice": [
        {{"character": "character_id", "style": "intro", "text": "What the character says"}}
      ]
    }},
    {{"song_index": 1, "voice": []}},
    {{
      "song_index": 2,
      "voice": [
        {{"character": "char_a", "style": "outro", "text": "First speaks"}},
        {{"character": "char_b", "style": "intro", "text": "Second responds"}},
        {{"character": "char_a", "style": "between_songs", "text": "First rebuts"}}
      ]
    }}
  ]
}}"""


async def generate_script(
    chat_backend: "ChatBackend",
    character: CharacterConfig,
    songs: list[dict],
    context: dict[str, str],
    active_characters: list[CharacterConfig] | None = None,
    signal: str = "buffer_low",
    seed: SeedState | None = None,
) -> Script:
    """Generate a structured multi-host script via a single LLM call."""
    active = active_characters or [character]
    user_message = _build_user_prompt(character, active, songs, context, signal, seed)

    try:
        response_text = await chat_backend.chat(user_message, system_prompt=SCRIPT_SYSTEM_PROMPT)
    except Exception:
        logger.exception(f"Script LLM call failed (backend={chat_backend.name}/{chat_backend.model})")
        return _silent_fallback(songs, character, active)

    return _parse_response(response_text, songs, character, active)


def _build_user_prompt(
    character: CharacterConfig,
    active_characters: list[CharacterConfig],
    songs: list[dict],
    context: dict[str, str],
    signal: str,
    seed: SeedState | None,
) -> str:
    parts: list[str] = []

    # Cast
    parts.append("[Cast]")
    parts.append(f"Primary: id={character.id} name=\"{character.name}\"")
    parts.append(f"Personality: {character.personality}")
    for ac in active_characters:
        if ac.id != character.id:
            parts.append(f"Co-host: id={ac.id} name=\"{ac.name}\"")
            parts.append(f"Personality: {ac.personality}")

    if len(active_characters) > 1:
        ids = ", ".join(f'"{c.id}"' for c in active_characters)
        parts.append(
            f"\nThis is a multi-host show. Hosts: {ids}. "
            "Use short alternating dialogue on some segments (ABAB patterns); keep others solo or silent."
        )

    # Seed
    if seed:
        parts.append(f"\n[Seed]")
        parts.append(f"Pipeline: {seed.pipeline}")
        if seed.mood_text:
            parts.append(f"Mood: {seed.mood_text}")
        if seed.genre_focus:
            parts.append(f"Genre focus: {', '.join(seed.genre_focus)}")
        if seed.first_song:
            parts.append(
                f"Kickoff song: {seed.first_song.get('artist','?')} — {seed.first_song.get('title','?')}"
            )
        if seed.interpretation_notes:
            parts.append(f"Producer note: {seed.interpretation_notes}")

    # Signal
    parts.append(f"\n[Signal: {signal}]")

    # Context
    if context:
        parts.append("\n[Context]")
        for key, value in context.items():
            parts.append(f"{key}: {value}")

    # Songs
    parts.append("\n[Song list (in order)]")
    for i, song in enumerate(songs):
        artist = song.get("artist", "?")
        title = song.get("title", "?")
        genre = song.get("genre", "?")
        year = song.get("year", "?")
        parts.append(f"{i}. {artist} — {title} (Genre: {genre}, Year: {year})")

    return "\n".join(parts)


def _parse_response(
    response_text: str,
    songs: list[dict],
    character: CharacterConfig,
    active_characters: list[CharacterConfig],
) -> Script:
    """Parse LLM JSON response into a Script."""
    json_str = _extract_json(response_text)

    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, TypeError):
        logger.warning("LLM returned invalid JSON, falling back to silent script")
        return _silent_fallback(songs, character, active_characters)

    known_ids = {c.id for c in active_characters}
    segments: list[ScriptSegment] = []
    raw_segments = data.get("segments", [])

    for i, seg_data in enumerate(raw_segments):
        song_idx = seg_data.get("song_index", i)
        if song_idx < 0 or song_idx >= len(songs):
            continue

        voice_cues: list[VoiceCue] = []
        for vc in seg_data.get("voice", [])[:MAX_EXCHANGES_PER_SEGMENT]:
            text = (vc.get("text") or "").strip()
            if not text:
                continue
            char_id = vc.get("character") or character.id
            if char_id not in known_ids:
                # Unknown/hallucinated character → attribute to primary
                char_id = character.id
            voice_cues.append(VoiceCue(
                character_id=char_id,
                style=vc.get("style", "intro"),
                text=text,
            ))

        segments.append(ScriptSegment(
            position=i,
            track=songs[song_idx],
            voice_cues=voice_cues,
        ))

    if not segments:
        return _silent_fallback(songs, character, active_characters)

    return Script(
        segments=segments,
        primary_character=character.id,
        cast=[c.id for c in active_characters],
        generated_at=time.time(),
    )


def _extract_json(text: str) -> str:
    """Extract JSON from LLM response, handling markdown fences."""
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        return match.group(1)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return match.group(0)
    return text


def _silent_fallback(
    songs: list[dict],
    character: CharacterConfig,
    active_characters: list[CharacterConfig],
) -> Script:
    """Create a script with songs but no voice — keeps music playing on LLM failure."""
    segments = [
        ScriptSegment(position=i, track=song, voice_cues=[], state="ready")
        for i, song in enumerate(songs)
    ]
    return Script(
        segments=segments,
        primary_character=character.id,
        cast=[c.id for c in active_characters],
        generated_at=time.time(),
    )
