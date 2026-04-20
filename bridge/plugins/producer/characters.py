"""Load character definitions from plugin config."""

from __future__ import annotations

import logging
from bridge.plugins.producer.models import CharacterConfig

logger = logging.getLogger(__name__)


def load_characters(config: dict) -> dict[str, CharacterConfig]:
    """Parse the 'characters' section from the producer plugin config.

    Expected config shape:
        characters:
          snoop:
            name: "Snoop Dogg"
            personality: "You are Snoop..."
            voice_speaker: "Adrian"
            ...
    """
    raw = config.get("characters", {})
    characters: dict[str, CharacterConfig] = {}

    for char_id, data in raw.items():
        if not isinstance(data, dict):
            logger.warning(f"Skipping character '{char_id}': expected dict, got {type(data)}")
            continue

        avoid = data.get("avoid_genres", [])
        if isinstance(avoid, str):
            avoid = [g.strip() for g in avoid.split(",") if g.strip()]

        genre_weights = data.get("genre_weights", {})
        if isinstance(genre_weights, str):
            import json
            try:
                genre_weights = json.loads(genre_weights)
            except (json.JSONDecodeError, TypeError):
                genre_weights = {}

        characters[char_id] = CharacterConfig(
            id=char_id,
            name=data.get("name", char_id),
            personality=data.get("personality", "You are a radio DJ."),
            voice_speaker=data.get("voice_speaker", "Aiden"),
            voice_instruct=data.get("voice_instruct", "Speak naturally"),
            genre_weights={k.lower(): float(v) for k, v in genre_weights.items()},
            avoid_genres={g.lower() for g in avoid},
        )
        logger.info(f"Loaded character: {char_id} ({characters[char_id].name})")

    return characters
