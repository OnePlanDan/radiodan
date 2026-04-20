"""Seed interpreter — turn any input into a SeedState.

Priority (explicit > interpreted):
    1. cast=[a,b] / character=x         → duo/character pipeline (no LLM)
    2. song=<path>                      → song pipeline (LLM picks host + genre_focus)
    3. genre=<name>                     → genre pipeline (code-only host pick)
    4. image=<file> / image_url=<url>   → vision → text → continue as text seed
    5. text=<anything>                  → LLM classifies into pipeline + parameters

Fallbacks keep the station playing:
    - Interpreter LLM fails → vibe pipeline carrying raw text as mood_text
    - Vision fails         → raises (caller returns 502 to client)
    - LLM picks unknown char → defaults to first configured character
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp

from bridge.plugins.producer.models import CharacterConfig, SeedState

if TYPE_CHECKING:
    from bridge.services.llm_backends import ChatBackend, VisionBackend

logger = logging.getLogger(__name__)


# =========================================================================
# Public entry point
# =========================================================================


async def interpret_seed(
    raw: dict,
    *,
    characters: dict[str, CharacterConfig],
    library: list[dict],
    interpreter: "ChatBackend",
    vision: "VisionBackend",
    upload_dir: Path,
) -> SeedState:
    """Normalise any seed into a SeedState."""
    if not characters:
        raise ValueError("No characters configured — cannot build a seed")

    # Explicit strict override from the request (None = use pipeline default)
    explicit_strict = _coerce_bool(raw.get("strict"))
    hard = bool(_coerce_bool(raw.get("hard")))

    # 1. Explicit cast or character (short-circuit, no LLM)
    cast = _coerce_cast(raw.get("cast"), characters)
    if not cast and raw.get("character"):
        if raw["character"] in characters:
            cast = [raw["character"]]

    if cast:
        pipeline = "duo" if len(cast) > 1 else "character"
        return SeedState(
            raw=_sanitise(raw),
            pipeline=pipeline,
            cast=cast,
            interpretation_notes=f"Host(s): {', '.join(characters[c].name for c in cast)}",
            strict=explicit_strict if explicit_strict is not None else False,
            hard=hard,
        )

    # 2. Explicit song (must exist in library)
    song_path = raw.get("song") or raw.get("song_path")
    if song_path:
        track = _find_track(library, song_path)
        if track:
            return await _song_seed(track, raw, characters, interpreter, explicit_strict)
        logger.warning(f"Seed song {song_path!r} not in library; falling through")

    # 3. Explicit genre
    if raw.get("genre"):
        genre = str(raw["genre"]).lower().strip()
        return _genre_seed(genre, raw, characters, explicit_strict)

    # 4. Image (upload or URL) → vision → text
    image_path = raw.get("_uploaded_image_path")  # set by route handler after save
    image_url = raw.get("image_url")
    if image_path or image_url:
        if image_url and not image_path:
            image_path = await _download_image(image_url, upload_dir)
        descr = await _describe_image(Path(image_path), vision)
        # Feed description into text pipeline for pipeline classification
        text_raw = {**raw, "text": descr, "_uploaded_image_path": str(image_path)}
        seed = await _text_seed(descr, text_raw, characters, interpreter, library, explicit_strict)
        seed.pipeline = "image"
        seed.uploaded_image_path = str(image_path)
        seed.interpretation_notes = f"Image → “{descr[:100]}” → {seed.interpretation_notes}"
        return seed

    # 5. Free-form text
    text = raw.get("text")
    if text:
        return await _text_seed(str(text), raw, characters, interpreter, library, explicit_strict)

    # Nothing given — default to first character, vibe pipeline
    first_char = next(iter(characters))
    logger.warning("Empty seed body, defaulting to first character")
    return SeedState(
        raw=_sanitise(raw),
        pipeline="character",
        cast=[first_char],
        interpretation_notes=f"No seed; defaulted to {characters[first_char].name}",
        strict=False,
        hard=hard,
    )


# =========================================================================
# Pipelines
# =========================================================================


async def _song_seed(
    track: dict,
    raw: dict,
    characters: dict[str, CharacterConfig],
    interpreter: "ChatBackend",
    explicit_strict: bool | None,
) -> SeedState:
    """LLM picks which host fits the song; genre_focus seeded from the track."""
    genre = (track.get("genre") or "").lower().strip()
    genre_focus = _expand_genre_synonyms(genre) if genre else []

    roster = _roster_summary(characters)
    prompt = (
        f"Song: {track.get('artist','?')} — {track.get('title','?')}"
        f" (genre: {genre or 'unknown'}, year: {track.get('year') or '?'})\n\n"
        f"Available hosts:\n{roster}\n\n"
        'Who would DJ this song? Reply JSON: {"character":"id","why":"one short reason"}'
    )
    cast, notes = await _pick_character(prompt, interpreter, characters)

    return SeedState(
        raw=_sanitise(raw),
        pipeline="song",
        cast=cast,
        first_song=track,
        genre_focus=genre_focus,
        interpretation_notes=f"Song-seed → {', '.join(characters[c].name for c in cast)}: {notes}",
        strict=explicit_strict if explicit_strict is not None else False,
    )


def _genre_seed(
    genre: str,
    raw: dict,
    characters: dict[str, CharacterConfig],
    explicit_strict: bool | None,
) -> SeedState:
    """Pick the host whose weights favour the genre most; no LLM."""
    focus = _expand_genre_synonyms(genre)
    best_id = None
    best_score = -1.0
    for cid, char in characters.items():
        score = 0.0
        for g, w in char.genre_weights.items():
            for f in focus:
                if f in g or g in f:
                    score = max(score, float(w))
        if score > best_score:
            best_score = score
            best_id = cid
    if best_id is None:
        best_id = next(iter(characters))
    return SeedState(
        raw=_sanitise(raw),
        pipeline="genre",
        cast=[best_id],
        genre_focus=focus,
        interpretation_notes=f"Genre '{genre}' → {characters[best_id].name}",
        strict=explicit_strict if explicit_strict is not None else True,
        hard=hard,
    )


async def _text_seed(
    text: str,
    raw: dict,
    characters: dict[str, CharacterConfig],
    interpreter: "ChatBackend",
    library: list[dict],
    explicit_strict: bool | None,
) -> SeedState:
    """Classify free-form text into pipeline + params via LLM."""
    roster = _roster_summary(characters)
    top_genres = _top_library_genres(library, limit=25)
    prompt = (
        f'Seed text: "{text}"\n\n'
        f"Available hosts:\n{roster}\n\n"
        f"Library genres (most common): {', '.join(top_genres)}\n\n"
        "Classify this seed and pick a pipeline. Reply JSON only, no markdown:\n"
        "{\n"
        '  "pipeline": "vibe|artist|genre|song",\n'
        '  "cast": ["host_id", ...],              // 1 or 2 hosts\n'
        '  "genre_focus": ["genre", ...],         // optional, from library genres\n'
        '  "mood_text": "one-line mood hint",     // for downstream script\n'
        '  "why": "one short reason"\n'
        "}"
    )

    try:
        raw_reply = await interpreter.chat(prompt, system_prompt=_INTERPRETER_SYSTEM)
        data = _extract_json(raw_reply)
    except Exception:
        logger.exception("Interpreter LLM failed; falling back to vibe pipeline")
        return SeedState(
            raw=_sanitise(raw),
            pipeline="vibe",
            cast=[next(iter(characters))],
            mood_text=text,
            interpretation_notes=f"LLM fail; raw mood: '{text[:60]}'",
            strict=explicit_strict if explicit_strict is not None else False,
            hard=hard,
        )

    cast = _coerce_cast(data.get("cast"), characters)
    if not cast:
        cast = [next(iter(characters))]

    pipeline = str(data.get("pipeline") or "vibe").lower()
    if pipeline not in {"vibe", "artist", "genre", "song"}:
        pipeline = "vibe"

    raw_focus = [
        g.lower() for g in (data.get("genre_focus") or [])
        if isinstance(g, str)
    ][:5]
    genre_focus: list[str] = []
    for g in raw_focus:
        for term in _expand_genre_synonyms(g):
            if term not in genre_focus:
                genre_focus.append(term)

    mood_text = data.get("mood_text") or text
    why = data.get("why") or ""

    return SeedState(
        raw=_sanitise(raw),
        pipeline=pipeline,
        cast=cast,
        genre_focus=genre_focus,
        mood_text=str(mood_text),
        interpretation_notes=f"{pipeline} → {', '.join(characters[c].name for c in cast)}: {why}",
        strict=explicit_strict if explicit_strict is not None else False,
    )


# =========================================================================
# Image helpers
# =========================================================================


async def _describe_image(path: Path, vision: "VisionBackend") -> str:
    prompt = (
        "You are helping pick the vibe for a radio show. "
        "Describe this image in 2-3 sentences: mood, colour palette, "
        "what's happening, what kind of music would fit. "
        "Just prose, no bullets or markdown."
    )
    return await vision.describe(path, prompt)


async def _download_image(url: str, upload_dir: Path) -> Path:
    upload_dir.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
            if response.status != 200:
                raise RuntimeError(f"image_url fetch failed ({response.status})")
            data = await response.read()
            if len(data) > 10 * 1024 * 1024:
                raise RuntimeError("image_url exceeds 10 MB limit")
            ext = _guess_ext(response.headers.get("Content-Type", ""), url)
            digest = hashlib.sha256(data).hexdigest()[:16]
            out = upload_dir / f"{digest}{ext}"
            out.write_bytes(data)
            return out


def _guess_ext(content_type: str, url: str) -> str:
    ct = content_type.lower()
    if "png" in ct:
        return ".png"
    if "webp" in ct:
        return ".webp"
    if "jpeg" in ct or "jpg" in ct:
        return ".jpg"
    for e in (".png", ".jpg", ".jpeg", ".webp"):
        if url.lower().endswith(e):
            return e if e != ".jpeg" else ".jpg"
    return ".bin"


# =========================================================================
# Shared helpers
# =========================================================================


_INTERPRETER_SYSTEM = (
    "You are a radio show producer's assistant. Classify listener input into "
    "a pipeline + show parameters. Always reply with valid JSON only — no "
    "prose, no markdown fences."
)


# Genre synonyms — expand a single user-typed term into a family of library-genre
# substrings so the strict filter catches variants ("hip-hop" finds "hip hop/rap",
# "rap" finds "hip-hop", "chip" finds "chiptune", "8-bit", etc.). Additive: the
# user term is always included first.
_GENRE_SYNONYMS: dict[str, list[str]] = {
    "hip-hop":   ["hip-hop", "hip hop", "rap"],
    "hiphop":    ["hip-hop", "hip hop", "rap"],
    "rap":       ["rap", "hip-hop", "hip hop"],
    "chip":      ["chip", "chiptune", "8-bit", "8bit", "demoscene", "tracker", "module"],
    "chiptune":  ["chiptune", "chip", "8-bit", "8bit", "demoscene", "tracker", "module"],
    "chip tune": ["chip tune", "chiptune", "chip", "8-bit", "8bit"],
    "chip tunes": ["chip tune", "chiptune", "chip", "8-bit", "8bit"],
    "8-bit":     ["8-bit", "8bit", "chiptune", "chip"],
    "8bit":      ["8bit", "8-bit", "chiptune", "chip"],
    "edm":       ["edm", "electronic", "dance", "house", "techno"],
    "house":     ["house", "deep house", "tech house"],
    "metal":     ["metal", "heavy metal", "thrash"],
    "classical": ["classical", "baroque", "orchestral"],
    "rock":      ["rock", "rock & roll", "rock/pop"],
    "pop":       ["pop", "rock/pop", "top 40"],
}


def _expand_genre_synonyms(term: str) -> list[str]:
    """Expand a genre term into a list of substrings for matching. Always returns at least [term]."""
    t = (term or "").lower().strip()
    if not t:
        return []
    if t in _GENRE_SYNONYMS:
        return list(_GENRE_SYNONYMS[t])
    return [t]


def _coerce_bool(value) -> bool | None:
    """Parse a truthy/falsy value into bool. Returns None if value is None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"true", "1", "yes", "y", "on"}:
            return True
        if s in {"false", "0", "no", "n", "off", ""}:
            return False
    return None


def _coerce_cast(value, characters: dict[str, CharacterConfig]) -> list[str]:
    """Normalise cast input. Returns [] if nothing valid."""
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        if isinstance(v, str) and v in characters and v not in out:
            out.append(v)
    return out[:4]  # safety cap


async def _pick_character(
    prompt: str,
    interpreter: "ChatBackend",
    characters: dict[str, CharacterConfig],
) -> tuple[list[str], str]:
    """LLM-backed single-character pick with fallback."""
    try:
        reply = await interpreter.chat(prompt, system_prompt=_INTERPRETER_SYSTEM)
        data = _extract_json(reply)
        cid = data.get("character")
        why = data.get("why", "")
        if isinstance(cid, str) and cid in characters:
            return [cid], str(why)
    except Exception:
        logger.exception("Interpreter pick failed")
    # Fallback to first character
    first = next(iter(characters))
    return [first], "fallback: interpreter unavailable"


def _extract_json(text: str) -> dict:
    """Parse JSON from LLM reply, tolerating markdown fences."""
    if not text:
        return {}
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        text = match.group(1)
    else:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            text = match.group(0)
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _find_track(library: list[dict], path_or_name: str) -> dict | None:
    """Match a seed song identifier against the library (exact path or filename)."""
    path_or_name = str(path_or_name)
    for t in library:
        if t.get("file_path") == path_or_name:
            return t
    fname = Path(path_or_name).name
    if fname:
        for t in library:
            if Path(t.get("file_path", "")).name == fname:
                return t
    return None


def _roster_summary(characters: dict[str, CharacterConfig]) -> str:
    lines = []
    for cid, c in characters.items():
        genres = ", ".join(sorted(c.genre_weights.keys())[:6]) or "(no preferences)"
        lines.append(f'- id="{cid}" name="{c.name}" genres=[{genres}]')
    return "\n".join(lines)


def _top_library_genres(library: list[dict], limit: int = 25) -> list[str]:
    counts: dict[str, int] = {}
    for t in library:
        g = (t.get("genre") or "").lower().strip()
        if not g:
            continue
        counts[g] = counts.get(g, 0) + 1
    return [g for g, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:limit]]


def _sanitise(raw: dict) -> dict:
    """Drop non-JSON-safe fields before storing on SeedState.raw."""
    out = {}
    for k, v in raw.items():
        if k.startswith("_"):
            continue
        if isinstance(v, (str, int, float, bool, list, dict)) or v is None:
            out[k] = v
    return out
