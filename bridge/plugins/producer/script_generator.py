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

# A bad response used to cost the whole rebuild cycle: one unparseable reply and
# the producer fell straight back to a silent script, so ~50 minutes of radio
# went out with no DJ. Across the journal that was 36 of ~631 builds (~6%).
# Nothing re-asked. Now each failure is retried with a corrective nudge.
DEFAULT_MAX_ATTEMPTS = 3

# Bound the salvage scan so a pathological response can't burn real time.
_MAX_SALVAGE_CUTS = 200


class ScriptParseError(Exception):
    """A response came back but could not be turned into a usable script."""

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
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Script:
    """Generate a structured multi-host script, re-asking on an unusable reply.

    Falling back to a silent script means ~50 minutes of radio with no DJ, so a
    bad reply is worth another round-trip. Each retry tells the model what was
    wrong with the previous attempt rather than blindly repeating the request.
    Only when every attempt fails do we accept silence.
    """
    active = active_characters or [character]
    base_prompt = _build_user_prompt(character, active, songs, context, signal, seed)
    attempts = max(1, max_attempts)
    last_reason = "unknown"

    for attempt in range(1, attempts + 1):
        prompt = base_prompt if attempt == 1 else f"{base_prompt}\n\n{_retry_note(last_reason)}"
        final_attempt = attempt == attempts

        try:
            response_text = await chat_backend.chat(prompt, system_prompt=SCRIPT_SYSTEM_PROMPT)
        except Exception as e:
            # Transient backend trouble also used to end the cycle immediately.
            last_reason = f"the request failed ({e})"
            logger.warning(
                f"Script LLM call failed on attempt {attempt}/{attempts} "
                f"(backend={chat_backend.name}/{chat_backend.model}): {e}"
            )
            continue

        try:
            script = _parse_response(
                response_text, songs, character, active,
                # The last attempt keeps a talk-free but structurally valid
                # script rather than throwing away a usable song order.
                require_voice=not final_attempt,
            )
        except ScriptParseError as e:
            last_reason = str(e)
            logger.warning(
                f"Script response unusable on attempt {attempt}/{attempts} "
                f"({e}; {len(response_text)} chars)"
            )
            continue

        if attempt > 1:
            logger.info(f"Script recovered on attempt {attempt}/{attempts}")
        return script

    logger.error(
        f"Script generation failed after {attempts} attempts ({last_reason}) — "
        "this segment goes out with no DJ"
    )
    return _silent_fallback(songs, character, active)


def _retry_note(reason: str) -> str:
    """Corrective instruction appended to the prompt on a retry."""
    return (
        f"[Retry] Your previous reply could not be used: {reason}. "
        "Respond with ONLY the JSON object described in the system prompt — no "
        "preamble, no explanation, no markdown fences. Emit the complete object "
        "including every closing bracket. Keep the talk text short so the whole "
        "response fits."
    )


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
    require_voice: bool = False,
) -> Script:
    """Parse an LLM JSON response into a Script.

    Args:
        require_voice: Treat a script with no talk at all as a failure. The
            system prompt asks for talk on 60-70% of songs, so a completely
            silent reply is a defect worth re-asking about — but the caller
            disables this on its final attempt so a usable song order isn't
            thrown away over missing talk.

    Raises:
        ScriptParseError: The response could not be turned into a usable script.
    """
    json_str = _extract_json(response_text)

    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, TypeError) as e:
        # Most likely a truncated reply. Recovering nine complete segments beats
        # discarding the whole cycle, so try closing it off before giving up.
        data = _salvage_json(json_str)
        if data is None:
            raise ScriptParseError(f"the JSON was invalid ({e})") from e
        logger.info(f"Salvaged a truncated script response ({len(json_str)} chars)")

    if not isinstance(data, dict):
        raise ScriptParseError(f"the JSON was a {type(data).__name__}, not an object")

    known_ids = {c.id for c in active_characters}
    segments: list[ScriptSegment] = []
    raw_segments = data.get("segments", [])

    if not isinstance(raw_segments, list):
        raise ScriptParseError("the \"segments\" value was not a list")

    for i, seg_data in enumerate(raw_segments):
        if not isinstance(seg_data, dict):
            continue
        song_idx = seg_data.get("song_index", i)
        if not isinstance(song_idx, int):
            continue
        if song_idx < 0 or song_idx >= len(songs):
            continue

        voice_cues: list[VoiceCue] = []
        raw_voice = seg_data.get("voice") or []
        if not isinstance(raw_voice, list):
            raw_voice = []
        for vc in raw_voice[:MAX_EXCHANGES_PER_SEGMENT]:
            if not isinstance(vc, dict):
                continue
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
        raise ScriptParseError("it contained no usable segments")

    if require_voice and not any(s.voice_cues for s in segments):
        raise ScriptParseError("every segment was silent — no talk at all")

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


def _salvage_json(text: str) -> dict | None:
    """Close off a truncated script response after its last complete segment.

    The schema is a flat {"segments": [...]} list, so a reply cut mid-flight can
    usually be rescued by trimming back to the last complete object and adding
    the missing brackets. Returns None when nothing usable comes out — this only
    ever runs after a strict parse has already failed.
    """
    start = text.find("{")
    if start < 0:
        return None
    body = text[start:]

    cut = len(body)
    for _ in range(_MAX_SALVAGE_CUTS):
        cut = body.rfind("}", 0, cut)
        if cut < 0:
            return None
        head = body[: cut + 1]
        for suffix in ("]}", "}]}", "", "}"):
            try:
                data = json.loads(head + suffix)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(data, dict) and isinstance(data.get("segments"), list) and data["segments"]:
                return data
    return None


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
