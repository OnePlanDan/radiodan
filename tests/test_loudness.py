"""Tests for per-track music loudness normalisation.

Measured across 1 364 library tracks on 2026-07-29: source loudness spans
-34.7 to +10.0 LUFS — a 44.7 dB spread. Songs jumped against each other and
against the DJ no matter how the master `music_vol` was trimmed, because a
master trim moves the average and leaves the spread untouched.
"""

import pytest

from bridge.audio.loudness import LoudnessScanner, gain_for

TARGET = -16.0
ASSUMED = -11.0


def g(loudness, peak=None, target=TARGET, boost=6.0, cut=30.0, ceiling=-1.0):
    return gain_for(
        loudness, target_lufs=target, assumed_lufs=ASSUMED,
        max_boost_db=boost, max_cut_db=cut,
        true_peak_dbfs=peak, peak_ceiling_dbfs=ceiling,
    )


# =====================================================================
# CUTTING — the common case
# =====================================================================

def test_hot_track_is_cut_to_target():
    assert g(-8.0, peak=-0.5) == pytest.approx(-8.0)


def test_track_already_at_target_is_left_alone():
    assert g(-16.0, peak=-3.0) == pytest.approx(0.0)


def test_pathologically_hot_track_gets_the_full_cut_it_needs():
    """The library contains a real track at +10.0 LUFS. A tight cut clamp would
    leave the single worst offender 14 dB too loud."""
    assert g(10.0, peak=0.5) == pytest.approx(-26.0)


def test_cut_is_still_bounded():
    assert g(10.0, peak=0.5, cut=12.0) == pytest.approx(-12.0)


# =====================================================================
# BOOSTING — bounded by the track's own headroom
# =====================================================================

def test_quiet_track_with_headroom_is_boosted():
    # -22 LUFS wants +6; peak at -9 dBFS allows up to +8.
    assert g(-22.0, peak=-9.0) == pytest.approx(6.0)


def test_boost_is_limited_by_true_peak_not_by_the_cap():
    """A quiet but peaky track must not be pushed into clipping."""
    # Wants +18.7, but peak is already -2 dBFS so only +1 dB of headroom exists.
    assert g(-34.7, peak=-2.0) == pytest.approx(1.0)


def test_no_boost_when_the_peak_is_already_at_the_ceiling():
    assert g(-30.0, peak=-1.0) == pytest.approx(0.0)


def test_no_negative_boost_when_peak_exceeds_the_ceiling():
    """A quiet-but-clipped track gets left alone rather than cut by the peak rule."""
    assert g(-30.0, peak=+0.5) == pytest.approx(0.0)


def test_unknown_peak_falls_back_to_the_conservative_cap():
    """Rows measured before true peak was recorded still behave safely."""
    assert g(-34.7, peak=None) == pytest.approx(6.0)


def test_cutting_ignores_peak_entirely():
    """Attenuation cannot clip, so headroom is irrelevant to it."""
    assert g(-8.0, peak=+2.0) == pytest.approx(-8.0)


# =====================================================================
# UNMEASURED TRACKS
# =====================================================================

def test_unmeasured_track_uses_the_assumed_level():
    """During a 7 700-file backfill, unmeasured tracks must not play raw."""
    assert g(None) == pytest.approx(TARGET - ASSUMED)


def test_assumed_level_matches_the_old_blanket_trim():
    """-5 dB is what music_vol 0.55 did, so behaviour is continuous while the
    backfill runs rather than lurching when it completes."""
    assert g(None) == pytest.approx(-5.0)


# =====================================================================
# TARGET
# =====================================================================

@pytest.mark.parametrize("target", [-14.0, -16.0, -18.0])
def test_target_is_honoured(target):
    assert g(-10.0, peak=-1.0, target=target) == pytest.approx(target + 10.0)


def test_two_very_different_tracks_land_at_the_same_level():
    """The whole point: a 5.5 dB source difference must come out flat."""
    hot, quiet = -8.9, -14.4
    assert hot + g(hot, peak=-0.8) == pytest.approx(quiet + g(quiet, peak=-3.0))


# =====================================================================
# SCANNER BOOKKEEPING
# =====================================================================

def test_pending_excludes_files_already_attempted():
    """A file that cannot be measured gets a NULL reading plus a timestamp. Keying
    'pending' on the reading alone made every batch re-fetch the same broken files
    forever — observed live as 5 200 'unmeasurable' with the remaining count
    frozen at 7 702."""
    where = LoudnessScanner._PENDING_WHERE
    assert "loudness_lufs IS NULL" in where
    assert "loudness_measured_at IS NULL" in where


def test_scanner_clamps_nonsense_settings():
    s = LoudnessScanner(db=None, concurrency=0, batch_size=0)
    assert s.concurrency >= 1
    assert s.batch_size >= 1
