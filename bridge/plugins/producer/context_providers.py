"""Context providers — gather parallel context before the AI script call."""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from bridge.plugins.producer.models import CharacterConfig

if TYPE_CHECKING:
    from bridge.plugins.base import PluginContext

logger = logging.getLogger(__name__)

# Selection scoring knobs (tier-B: recency + fatigue)
RECENCY_FULL_HOURS = 72.0   # Fully "cooled off" after this many hours since last play
FATIGUE_COEFF = 0.1         # Per effective-play weight decay: 1 / (1 + k · plays)

# Type for context provider functions
ContextProvider = Callable[["PluginContext", CharacterConfig, dict], Awaitable[dict[str, str]]]

# Registry of named providers
_providers: dict[str, ContextProvider] = {}


def register_provider(name: str):
    """Decorator to register a context provider."""
    def wrapper(fn: ContextProvider):
        _providers[name] = fn
        return fn
    return wrapper


async def gather_context(
    ctx: "PluginContext",
    character: CharacterConfig,
    producer_config: dict | None = None,
    providers: list[str] | None = None,
) -> dict[str, str]:
    """Run all (or specified) providers in parallel, return merged context dict.

    Individual provider failures are logged but don't block others.
    """
    cfg = producer_config or {}
    to_run = providers or list(_providers.keys())
    tasks = {}
    for name in to_run:
        if name in _providers:
            tasks[name] = _providers[name](ctx, character, cfg)

    if not tasks:
        return {}

    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    context: dict[str, str] = {}
    for name, result in zip(tasks.keys(), results):
        if isinstance(result, Exception):
            logger.warning(f"Context provider '{name}' failed: {result}")
        elif isinstance(result, dict):
            context.update(result)
    return context


# =========================================================================
# Built-in providers
# =========================================================================


@register_provider("datetime")
async def provide_datetime(ctx: "PluginContext", character: CharacterConfig, cfg: dict) -> dict[str, str]:
    now = datetime.now()
    hour = now.hour
    if hour < 6:
        day_part = "late night"
    elif hour < 12:
        day_part = "morning"
    elif hour < 17:
        day_part = "afternoon"
    elif hour < 21:
        day_part = "evening"
    else:
        day_part = "night"

    return {
        "date": now.strftime("%A %B %d, %Y"),
        "time": now.strftime("%H:%M"),
        "day_part": day_part,
    }


@register_provider("recent_plays")
async def provide_recent_plays(ctx: "PluginContext", character: CharacterConfig, cfg: dict) -> dict[str, str]:
    planner = ctx.playlist_planner
    if not planner:
        return {"recent_plays": "No play history available."}

    history = await planner.get_history(limit=10)
    if not history:
        return {"recent_plays": "No recent plays."}

    lines = []
    for h in history:
        fp = h.get("file_path", "")
        # Try to find metadata from library
        artist = "?"
        title = "?"
        for t in planner.library:
            if t["file_path"] == fp:
                artist = t.get("artist", "?")
                title = t.get("title", "?")
                break
        lines.append(f"- {artist} — {title}")

    return {"recent_plays": "\n".join(lines)}


@register_provider("feeder_context")
async def provide_feeder_context(ctx: "PluginContext", character: CharacterConfig, cfg: dict) -> dict[str, str]:
    """Pass through any enrichment data from ContextFeeder plugins."""
    fc = ctx.stream_context.feeder_context if ctx.stream_context else {}
    if not fc:
        return {}
    return {k: str(v)[:200] for k, v in fc.items()}


# =========================================================================
# Song selection (not a context provider, but used by the producer)
# =========================================================================


def select_songs(
    library: list[dict],
    character: CharacterConfig,
    count: int,
    history_paths: set[str],
    upcoming_paths: set[str],
) -> list[dict]:
    """Genre-weighted song selection from library.

    Ported from CharacterPlugin._select_track, applied N times.
    """
    songs: list[dict] = []
    used: set[str] = set(history_paths) | set(upcoming_paths)

    for _ in range(count):
        track = _select_one(library, character, used)
        if track:
            songs.append(track)
            used.add(track["file_path"])

    return songs


def _select_one(
    library: list[dict],
    character: CharacterConfig,
    used: set[str],
) -> dict | None:
    """Select a single track, weighted by genre × recency × fatigue.

    Weight = genre_weight(char) × recency_decay(last_played_at) × fatigue(play_count + play_bias)
      - recency_decay climbs from 0 (just played) to 1 (never played or ≥ RECENCY_FULL_HOURS ago)
      - fatigue = 1 / (1 + FATIGUE_COEFF × effective_plays), pulling popular tracks down
      - play_bias (signed) lets humans force-cool or force-surface without touching real plays
    """
    now = datetime.now(timezone.utc)
    candidates = []
    weights = []

    for track in library:
        fp = track["file_path"]
        if fp in used:
            continue
        genre = (track.get("genre") or "").lower().strip()
        if genre in character.avoid_genres:
            continue

        # Genre weight from character's preferences
        if character.genre_weights:
            w = 0.0
            for g, gw in character.genre_weights.items():
                if g in genre:
                    w = max(w, gw)
            if w == 0:
                w = 0.5  # Baseline for genres not in preferences
        else:
            w = 1.0  # No preference = equal weight

        # Recency decay
        last = track.get("last_played_at")
        if last:
            try:
                hours = (now - datetime.fromisoformat(last)).total_seconds() / 3600.0
                w *= max(0.0, min(1.0, hours / RECENCY_FULL_HOURS))
            except ValueError:
                pass  # Unparseable timestamp → treat as never played

        # Fatigue on effective play count
        eff = max(0, (track.get("play_count") or 0) + (track.get("play_bias") or 0))
        w *= 1.0 / (1.0 + FATIGUE_COEFF * eff)

        if w <= 0:
            continue
        candidates.append(track)
        weights.append(w)

    if not candidates:
        # Fallback: anything not already used
        fallback = [t for t in library if t["file_path"] not in used]
        return random.choice(fallback) if fallback else None

    return random.choices(candidates, weights=weights, k=1)[0]
