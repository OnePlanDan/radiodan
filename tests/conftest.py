"""Shared fixtures for RadioDan timeline tests."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from bridge.event_store import EventStore
from bridge.web.routes.timeline import routes as timeline_routes


@pytest.fixture
async def event_store():
    """In-memory EventStore, opened and closed per test."""
    store = EventStore(db_path=Path(":memory:"))
    await store.open()
    yield store
    await store.close()


@pytest.fixture
def mock_stream_context():
    """Mock StreamContext with fixed timing values."""
    ctx = MagicMock()
    ctx.elapsed_seconds = 42.5
    ctx.remaining_seconds = 197.3
    ctx._planner = None
    ctx.upcoming_tracks = []
    return ctx


@pytest.fixture
def timeline_app(event_store, mock_stream_context):
    """Minimal aiohttp app wired with timeline routes + fixtures."""
    app = web.Application()
    app["event_store"] = event_store
    app["stream_context"] = mock_stream_context
    app.router.add_routes(timeline_routes)
    return app


async def read_sse_frames(response, count=1, timeout=2.0):
    """Read `count` SSE frames from a streaming response.

    Each frame is returned as a dict with 'event' and 'data' keys.
    """
    frames = []
    buffer = b""

    async def _read():
        nonlocal buffer
        while len(frames) < count:
            chunk = await response.content.read(4096)
            if not chunk:
                break
            buffer += chunk
            # Split on double-newline (SSE frame separator)
            while b"\n\n" in buffer:
                raw_frame, buffer = buffer.split(b"\n\n", 1)
                frame = _parse_sse_frame(raw_frame.decode())
                if frame:
                    frames.append(frame)
                    if len(frames) >= count:
                        return

    try:
        await asyncio.wait_for(_read(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return frames


def _parse_sse_frame(raw: str) -> dict | None:
    """Parse a single SSE frame into {event, data}."""
    event = None
    data_lines = []
    for line in raw.strip().split("\n"):
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
        elif line.startswith(":"):
            # Comment line (e.g. keepalive)
            return {"event": "comment", "data": line[1:].strip()}
    if event is None and not data_lines:
        return None
    return {"event": event, "data": "\n".join(data_lines)}
