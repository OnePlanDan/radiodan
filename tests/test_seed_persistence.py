"""Tests for the live seed surviving a restart.

The seed lived only in `self._seed` on the producer instance, and `on_start`
unconditionally queued the default host. So any reboot or redeploy silently
dropped the show back to default Bob — a strict hip-hop seed set hours earlier
was simply gone, with nothing in the log to say so. It happened eight times in a
single session of development restarts.
"""

import asyncio
import logging

import pytest

from bridge.plugins.base import PluginContext
from bridge.plugins.producer import plugin as producer_plugin
from bridge.plugins.producer.models import SeedState
from bridge.plugins.producer.plugin import ProducerPlugin


class FakeStore:
    """Stand-in ConfigStore with the same get/set contract."""

    def __init__(self, initial=None):
        self.data = dict(initial or {})
        self.writes = 0

    async def get(self, section, key, default=None):
        return self.data.get((section, key), default)

    async def set(self, section, key, value):
        self.writes += 1
        self.data[(section, key)] = value


@pytest.fixture
def producer():
    p = object.__new__(ProducerPlugin)
    p.logger = logging.getLogger("test.producer")
    p.instance_id = "default-producer"
    p._seed = None
    p._characters = {"bob": object(), "lani": object()}
    p._interpreter_backend = None
    p._vision_backend = None
    p._upload_dir = None
    p._signal_queue = asyncio.Queue()
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
    p.ctx = PluginContext(
        tts_service=None, mixer=None, llm_service=None,
        stream_context=None, voice_scheduler=None,
        config={"default_character": "bob"}, config_store=FakeStore(),
    )
    return p


def _genre_seed():
    return SeedState(
        pipeline="genre", cast=["bob"], genre_focus=["hip-hop", "rap"],
        interpretation_notes="Genre 'hip-hop' → Bad Mouth Bob",
        strict=True, hard=True, raw={"genre": "hip-hop", "hard": True},
    )


# =====================================================================
# STORAGE ROUND TRIP
# =====================================================================

def test_round_trip_keeps_what_defines_the_show():
    restored = SeedState.from_storage(_genre_seed().to_storage())
    assert restored.pipeline == "genre"
    assert restored.genre_focus == ["hip-hop", "rap"]
    assert restored.cast == ["bob"]
    assert restored.strict is True
    assert restored.interpretation_notes == "Genre 'hip-hop' → Bad Mouth Bob"


def test_hard_is_not_restored_armed():
    """`hard` cuts whatever is playing. Restoring it armed would chop a song on
    every single restart."""
    restored = SeedState.from_storage(_genre_seed().to_storage())
    assert restored.hard is False
    assert restored.hard_consumed is True


def test_restored_seed_reads_as_freshly_applied():
    """Reviving the old set_at would resurrect stale age arithmetic — the same
    defect that once logged a build as taking 2 023 895 seconds."""
    import time
    old = _genre_seed()
    old.set_at = time.time() - 30 * 86400
    old.built_at = old.set_at + 20
    restored = SeedState.from_storage(old.to_storage())
    assert restored.set_at == pytest.approx(time.time(), abs=5)
    assert restored.built_at is None
    assert restored.songs_queued_at is None
    assert restored.live_at is None


def test_uploaded_image_blob_is_not_stored():
    seed = SeedState(pipeline="image", raw={"image": b"x" * 1000, "note": "keep"})
    stored = seed.to_storage()
    assert "image" not in stored["raw"]
    assert stored["raw"]["note"] == "keep"


def test_unknown_stored_keys_are_ignored():
    """A seed written by a newer build must not break an older one."""
    data = _genre_seed().to_storage()
    data["some_future_field"] = 123
    restored = SeedState.from_storage(data)
    assert restored.pipeline == "genre"


def test_storage_excludes_timing_and_hard():
    keys = set(_genre_seed().to_storage())
    assert "set_at" not in keys
    assert "built_at" not in keys
    assert "hard" not in keys


# =====================================================================
# PERSIST ON CHANGE
# =====================================================================

async def test_seed_is_persisted_when_it_goes_live(producer, monkeypatch):
    seed = _genre_seed()
    monkeypatch.setattr(producer_plugin, "interpret_seed",
                        lambda *a, **k: _async(seed))
    await producer._handle_seed({"genre": "hip-hop"})

    store = producer.ctx.config_store
    assert store.writes == 1
    saved = store.data[("producer", "seed:default-producer")]
    assert saved["genre_focus"] == ["hip-hop", "rap"]


async def test_boot_default_is_not_persisted(producer, monkeypatch):
    """Otherwise the default would overwrite a real seed on every start."""
    monkeypatch.setattr(producer_plugin, "interpret_seed",
                        lambda *a, **k: _async(SeedState(pipeline="character")))
    await producer._handle_seed({"cast": ["bob"], "_silent_default": True})
    assert producer.ctx.config_store.writes == 0


async def test_a_storage_failure_does_not_break_seeding(producer, monkeypatch):
    class Broken(FakeStore):
        async def set(self, *a, **k):
            raise RuntimeError("disk on fire")

    producer.ctx.config_store = Broken()
    monkeypatch.setattr(producer_plugin, "interpret_seed",
                        lambda *a, **k: _async(_genre_seed()))
    await producer._handle_seed({"genre": "hip-hop"})
    assert producer._seed.pipeline == "genre", "the show still changed"


# =====================================================================
# RESTORE AT BOOT
# =====================================================================

async def test_restored_seed_skips_the_interpreter(producer, monkeypatch):
    """No LLM call at boot, and no chance of resolving into a different show."""
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("interpreter must not run for a restored seed")

    monkeypatch.setattr(producer_plugin, "interpret_seed", _boom)
    await producer._handle_seed({"_restored": _genre_seed().to_storage()})

    assert called["n"] == 0
    assert producer._seed.pipeline == "genre"
    assert producer._seed.genre_focus == ["hip-hop", "rap"]


async def test_restore_is_quiet(producer, monkeypatch):
    """Resuming the same show is not a handover: no flush, no hard skip."""
    monkeypatch.setattr(producer_plugin, "interpret_seed", lambda *a, **k: _async(None))
    await producer._handle_seed({"_restored": _genre_seed().to_storage()})
    assert producer.calls == ["build_script"], "no soft flush on a restore"
    assert producer.build_apply_hard is False


async def test_restore_does_not_rewrite_storage(producer, monkeypatch):
    monkeypatch.setattr(producer_plugin, "interpret_seed", lambda *a, **k: _async(None))
    await producer._handle_seed({"_restored": _genre_seed().to_storage()})
    assert producer.ctx.config_store.writes == 0


async def test_corrupt_stored_seed_falls_back_to_default(producer, monkeypatch):
    """A bad config row must not leave the station with no show at all."""
    monkeypatch.setattr(producer_plugin, "interpret_seed", lambda *a, **k: _async(None))
    await producer._handle_seed({"_restored": {"cast": "not-a-list", "pipeline": object()}})

    queued = await asyncio.wait_for(producer._signal_queue.get(), timeout=1)
    assert queued[0] == "seed"
    assert queued[1]["_silent_default"] is True


async def test_load_returns_none_when_nothing_saved(producer):
    assert await producer._load_seed() is None


async def test_load_ignores_an_empty_row(producer):
    producer.ctx.config_store.data[("producer", "seed:default-producer")] = {}
    assert await producer._load_seed() is None


async def test_persistence_is_per_instance(producer, monkeypatch):
    """Two producer instances must not share one seed."""
    monkeypatch.setattr(producer_plugin, "interpret_seed",
                        lambda *a, **k: _async(_genre_seed()))
    await producer._handle_seed({"genre": "hip-hop"})
    assert ("producer", "seed:default-producer") in producer.ctx.config_store.data


async def test_missing_config_store_is_tolerated(producer, monkeypatch):
    """Older wiring, or a plugin context built without one."""
    producer.ctx.config_store = None
    monkeypatch.setattr(producer_plugin, "interpret_seed",
                        lambda *a, **k: _async(_genre_seed()))
    await producer._handle_seed({"genre": "hip-hop"})
    assert producer._seed.pipeline == "genre"
    assert await producer._load_seed() is None


def _async(value):
    async def _coro(*a, **k):
        return value
    return _coro()
