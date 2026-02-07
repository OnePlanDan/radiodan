"""Smoke tests for timeline instrumentation wiring across services."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bridge.event_store import EventStore


# The 5 service classes that should have set_event_store()
SERVICE_CLASSES = [
    "bridge.audio.stream_context.StreamContext",
    "bridge.audio.voice_scheduler.VoiceScheduler",
    "bridge.audio.playlist_planner.PlaylistPlanner",
    "bridge.services.tts_service.TTSService",
    "bridge.services.llm_service.LLMService",
]


def _import_class(dotted_path: str):
    """Import a class from a dotted module path."""
    module_path, class_name = dotted_path.rsplit(".", 1)
    import importlib
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def test_all_services_have_set_event_store():
    """All 5 service classes should have a set_event_store() method."""
    for path in SERVICE_CLASSES:
        cls = _import_class(path)
        assert hasattr(cls, "set_event_store"), f"{path} missing set_event_store()"
        assert callable(getattr(cls, "set_event_store"))


async def test_set_event_store_sets_attribute(event_store):
    """set_event_store() should set the _event_store attribute on each service."""
    from bridge.audio.stream_context import StreamContext
    from bridge.audio.voice_scheduler import VoiceScheduler
    from bridge.audio.playlist_planner import PlaylistPlanner
    from bridge.services.tts_service import TTSService

    # StreamContext needs a mixer
    mock_mixer = MagicMock()
    ctx = StreamContext(mixer=mock_mixer)
    ctx.set_event_store(event_store)
    assert ctx._event_store is event_store

    # VoiceScheduler needs tts_service, mixer, stream_context
    mock_tts = MagicMock()
    scheduler = VoiceScheduler(tts_service=mock_tts, mixer=mock_mixer, stream_context=ctx)
    scheduler.set_event_store(event_store)
    assert scheduler._event_store is event_store

    # PlaylistPlanner needs mixer, db_path, music_dir
    planner = PlaylistPlanner(
        mixer=mock_mixer,
        db_path=Path(":memory:"),
        music_dir=Path("/tmp/music"),
    )
    planner.set_event_store(event_store)
    assert planner._event_store is event_store

    # TTSService needs endpoint, cache_dir
    tts = TTSService(endpoint="http://localhost:42001/tts", cache_dir=Path("/tmp/tts"))
    tts.set_event_store(event_store)
    assert tts._event_store is event_store


async def test_stream_context_poll_creates_track_play_event(event_store):
    """StreamContext._poll_once() should create a track_play event on track change."""
    from bridge.audio.stream_context import StreamContext

    mock_mixer = MagicMock()
    mock_mixer.get_track_info = AsyncMock(return_value={
        "artist": "Test Artist",
        "title": "Test Title",
        "filename": "/music/test.mp3",
        "genre": "",
        "year": "",
        "album": "",
    })
    mock_mixer.get_remaining = AsyncMock(return_value=180.0)
    mock_mixer.get_elapsed = AsyncMock(return_value=5.0)

    ctx = StreamContext(mixer=mock_mixer)
    ctx.set_event_store(event_store)

    # Suppress booth log output during test
    with patch("bridge.audio.stream_context.booth"):
        await ctx._poll_once()

    # Verify a track_play event was created
    events = await event_store.get_window(0.0, time.time() + 100)
    assert len(events) == 1
    assert events[0]["event_type"] == "track_play"
    assert events[0]["lane"] == "music"
    assert "Test Artist" in events[0]["title"]
    assert "Test Title" in events[0]["title"]
    assert events[0]["details"]["filename"] == "/music/test.mp3"


# Need time for the e2e test
import time
