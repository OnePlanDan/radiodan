"""The producer side of the seed ack: _handle_seed must always resolve it.

An unresolved ack means the HTTP caller waits out its whole timeout and is then
told "pending" when the seed had in fact already been applied or rejected.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from bridge.plugins.producer import plugin as producer_plugin
from bridge.plugins.producer.models import SeedState
from bridge.plugins.producer.plugin import ProducerPlugin


@pytest.fixture
def handler(monkeypatch):
    """A ProducerPlugin with only what _handle_seed touches wired up."""
    p = object.__new__(ProducerPlugin)
    p.logger = logging.getLogger("test.producer")
    p._seed = None
    p._characters = {}
    p._interpreter_backend = None
    p._vision_backend = None
    p._upload_dir = None
    p.calls = []

    p._library = lambda: []
    p._primary_char = lambda: None
    p.create_task = lambda coro: coro.close()

    async def _soft_flush():
        p.calls.append("soft_flush")

    async def _build_script(*, apply_hard=False):
        p.calls.append("build_script")
        p.build_apply_hard = apply_hard

    p._soft_flush = _soft_flush
    p._build_script = _build_script
    p.build_apply_hard = None
    # _handle_seed persists the seed via ctx.config_store; None means "don't".
    p.ctx = SimpleNamespace(config_store=None, config={})
    p._signal_queue = asyncio.Queue()
    return p


def _interpret_returns(seed):
    async def _fake(payload, **kwargs):
        return seed
    return _fake


def _interpret_raises(exc):
    async def _fake(payload, **kwargs):
        raise exc
    return _fake


async def test_ack_resolves_applied_once_the_seed_is_live(handler, monkeypatch):
    seed = SeedState(pipeline="genre")
    monkeypatch.setattr(producer_plugin, "interpret_seed", _interpret_returns(seed))

    ack = asyncio.get_running_loop().create_future()
    await handler._handle_seed({"genre": "hip-hop", "_ack": ack})

    assert ack.done()
    assert ack.result() == {"applied": True, "error": ""}
    assert handler._seed is seed
    assert handler.calls == ["soft_flush", "build_script"]
    assert handler.build_apply_hard is True, "a real seed build may honour hard"


async def test_ack_resolves_before_the_slow_phase_two_build(handler, monkeypatch):
    """The caller must not be held open for a 30-90s script build."""
    seed = SeedState(pipeline="genre")
    monkeypatch.setattr(producer_plugin, "interpret_seed", _interpret_returns(seed))

    resolved_during_build = {}

    async def _slow_build(*, apply_hard=False):
        resolved_during_build["done"] = ack.done()
        handler.calls.append("build_script")

    handler._build_script = _slow_build

    ack = asyncio.get_running_loop().create_future()
    await handler._handle_seed({"genre": "hip-hop", "_ack": ack})

    assert resolved_during_build["done"] is True


async def test_ack_reports_interpretation_failure(handler, monkeypatch):
    monkeypatch.setattr(
        producer_plugin, "interpret_seed", _interpret_raises(RuntimeError("boom")))

    ack = asyncio.get_running_loop().create_future()
    await handler._handle_seed({"genre": "hip-hop", "_ack": ack})

    result = ack.result()
    assert result["applied"] is False
    assert "boom" in result["error"]
    assert handler._seed is None, "the previous seed must be kept"
    assert handler.calls == [], "no flush or rebuild on a rejected seed"


async def test_ack_resolves_even_when_the_build_explodes(handler, monkeypatch):
    """Applied is applied — a later build failure must not strand the caller."""
    seed = SeedState(pipeline="genre")
    monkeypatch.setattr(producer_plugin, "interpret_seed", _interpret_returns(seed))

    async def _boom(*, apply_hard=False):
        raise RuntimeError("build died")

    handler._build_script = _boom

    ack = asyncio.get_running_loop().create_future()
    with pytest.raises(RuntimeError, match="build died"):
        await handler._handle_seed({"genre": "hip-hop", "_ack": ack})

    assert ack.result() == {"applied": True, "error": ""}


async def test_ack_resolves_when_soft_flush_explodes(handler, monkeypatch):
    """A failure before the seed goes live must still answer the caller."""
    seed = SeedState(pipeline="genre")
    monkeypatch.setattr(producer_plugin, "interpret_seed", _interpret_returns(seed))

    async def _boom():
        raise RuntimeError("liquidsoap gone")

    handler._soft_flush = _boom

    ack = asyncio.get_running_loop().create_future()
    with pytest.raises(RuntimeError, match="liquidsoap gone"):
        await handler._handle_seed({"genre": "hip-hop", "_ack": ack})

    assert ack.done(), "the caller must never be left hanging"
    assert ack.result()["applied"] is False


async def test_no_ack_is_harmless(handler, monkeypatch):
    """Internal seeds (the initial default) submit without an ack."""
    seed = SeedState(pipeline="character")
    monkeypatch.setattr(producer_plugin, "interpret_seed", _interpret_returns(seed))

    await handler._handle_seed({"cast": ["bob"], "_silent_default": True})
    assert handler._seed is seed
    # silent_default skips the flush
    assert handler.calls == ["build_script"]
    assert handler.build_apply_hard is False, "a silent bootstrap must never cut a track"


async def test_ack_is_stripped_before_interpretation(handler, monkeypatch):
    """The ack must not leak into the seed payload the interpreter sees."""
    seen = {}

    async def _fake(payload, **kwargs):
        seen.update(payload)
        return SeedState(pipeline="genre")

    monkeypatch.setattr(producer_plugin, "interpret_seed", _fake)

    ack = asyncio.get_running_loop().create_future()
    await handler._handle_seed({"genre": "hip-hop", "_ack": ack})

    assert "_ack" not in seen
    assert seen == {"genre": "hip-hop"}
