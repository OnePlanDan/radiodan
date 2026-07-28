"""The seed's `hard` flag must skip a track exactly once.

Found 2026-07-27: `hard` was re-read on every script build. The producer rebuilds
its rolling script roughly every 50 minutes, so a single `{"hard": true}` seed cut
a track mid-song every cycle for as long as it stayed live — 23 days, in the case
found. Nothing ever reset the flag.
"""

import logging

import pytest

from bridge.plugins.producer.models import SeedState
from bridge.plugins.producer.plugin import ProducerPlugin


class FakeMixer:
    def __init__(self, explode=False):
        self.skips = 0
        self.explode = explode

    async def next_track(self):
        self.skips += 1
        if self.explode:
            raise RuntimeError("liquidsoap said no")


class FakeCtx:
    def __init__(self, mixer):
        self.mixer = mixer


@pytest.fixture
def producer():
    p = object.__new__(ProducerPlugin)
    p.logger = logging.getLogger("test.producer")
    p._seed = None
    p.ctx = FakeCtx(FakeMixer())
    return p


def _hard_seed():
    return SeedState(pipeline="genre", hard=True, raw={"genre": "hip-hop", "hard": True})


# =====================================================================
# THE REGRESSION
# =====================================================================

async def test_rolling_rebuild_never_hard_skips(producer):
    """buffer_low rebuilds pass apply_hard=False — this is the actual bug."""
    producer._seed = _hard_seed()

    for _ in range(30):  # ~25 hours of rebuild cycles
        await producer._maybe_hard_skip(apply_hard=False)

    assert producer.ctx.mixer.skips == 0
    assert producer._seed.hard_consumed is False, "an unfired one-shot stays armed"


async def test_seed_build_skips_exactly_once(producer):
    producer._seed = _hard_seed()

    await producer._maybe_hard_skip(apply_hard=True)
    assert producer.ctx.mixer.skips == 1

    # Even if a future caller passes the flag again, it's spent.
    for _ in range(5):
        await producer._maybe_hard_skip(apply_hard=True)
    assert producer.ctx.mixer.skips == 1
    assert producer._seed.hard_consumed is True


async def test_the_23_day_scenario(producer):
    """One hard seed, then weeks of rolling rebuilds: exactly one cut."""
    producer._seed = _hard_seed()

    await producer._maybe_hard_skip(apply_hard=True)      # the seed build
    for _ in range(660):                                  # ~23 days at ~28/day
        await producer._maybe_hard_skip(apply_hard=False)

    assert producer.ctx.mixer.skips == 1


# =====================================================================
# NON-HARD SEEDS
# =====================================================================

async def test_soft_seed_never_skips(producer):
    producer._seed = SeedState(pipeline="genre", hard=False)
    await producer._maybe_hard_skip(apply_hard=True)
    assert producer.ctx.mixer.skips == 0


async def test_no_seed_is_a_no_op(producer):
    producer._seed = None
    await producer._maybe_hard_skip(apply_hard=True)
    assert producer.ctx.mixer.skips == 0


async def test_a_new_hard_seed_gets_its_own_skip(producer):
    """Consumption is per-seed, not global."""
    producer._seed = _hard_seed()
    await producer._maybe_hard_skip(apply_hard=True)

    producer._seed = _hard_seed()  # operator seeds again with hard
    await producer._maybe_hard_skip(apply_hard=True)

    assert producer.ctx.mixer.skips == 2


# =====================================================================
# FAILURE HANDLING
# =====================================================================

async def test_failed_skip_is_swallowed_and_not_retried_later(producer):
    """A half-failed skip must not leave the flag armed to cut a track later."""
    producer.ctx = FakeCtx(FakeMixer(explode=True))
    producer._seed = _hard_seed()

    await producer._maybe_hard_skip(apply_hard=True)  # must not raise
    assert producer._seed.hard_consumed is True

    producer.ctx.mixer.explode = False
    await producer._maybe_hard_skip(apply_hard=True)
    assert producer.ctx.mixer.skips == 1, "no surprise cut on a later build"


# =====================================================================
# API SURFACE
# =====================================================================

def test_status_reports_requested_and_consumed_separately():
    seed = _hard_seed()
    assert seed.as_dict()["hard"] is True
    assert seed.as_dict()["hard_consumed"] is False

    seed.hard_consumed = True
    d = seed.as_dict()
    assert d["hard"] is True, "the request is still visible"
    assert d["hard_consumed"] is True, "and so is the fact that it fired"
    assert d["raw"]["hard"] is True


def test_build_script_defaults_to_not_applying_hard():
    """The safe default matters: three of four call sites rely on it."""
    import inspect
    sig = inspect.signature(ProducerPlugin._build_script)
    assert sig.parameters["apply_hard"].default is False
    assert sig.parameters["apply_hard"].kind is inspect.Parameter.KEYWORD_ONLY


def test_only_the_seed_handler_passes_apply_hard():
    """Guards the invariant structurally rather than trusting a comment."""
    import inspect
    source = inspect.getsource(ProducerPlugin)
    calls = [ln.strip() for ln in source.splitlines() if "_build_script(" in ln
             and "def _build_script" not in ln]
    with_hard = [c for c in calls if "apply_hard" in c]
    assert len(calls) == 4, f"call sites changed, re-check the invariant: {calls}"
    assert len(with_hard) == 1, f"exactly one build may hard-skip, got: {with_hard}"
    assert "not silent_default" in with_hard[0]
