"""Tests for voice loudness normalisation and the hourly chime wording.

Measured on air 2026-07-29: voice was airing at -16.1 LUFS against music at
-7.6 LUFS with +0.7 dBFS true peak. The DJ was ~8.5 dB under the songs and
inaudible at low listening volume, and the stream was clipping.

Raising the loudnorm target alone does not fix it — single-pass and two-pass both
saturate near -13.4 LUFS because the true-peak ceiling binds first. Compression
ahead of loudnorm is what makes the requested gain fit.
"""

from pathlib import Path

import pytest

from bridge.plugins.dong import DongPlugin
from bridge.services.tts_service import TTSService


def _svc(**kwargs):
    kwargs.setdefault("endpoint", "http://127.0.0.1:1/tts")
    kwargs.setdefault("cache_dir", Path("/tmp"))
    return TTSService(**kwargs)


# =====================================================================
# VOICE LOUDNESS
# =====================================================================

def test_default_target_is_louder_than_the_old_hardcoded_minus_16():
    """-16 LUFS is what left the DJ buried under the music."""
    assert _svc().loudness_target > -16.0


def test_filter_compresses_before_loudnorm():
    """Order matters: loudnorm cannot reach the target unless the crest factor
    has already come down, because true peak binds first."""
    chain = _svc()._normalize_filter()
    assert chain.index("acompressor") < chain.index("loudnorm"), chain


def test_filter_carries_the_configured_values():
    chain = _svc(
        loudness_target=-11.0, true_peak=-2.0,
        compress_threshold="-20dB", compress_ratio=4.0,
    )._normalize_filter()
    assert "I=-11.0" in chain
    assert "TP=-2.0" in chain
    assert "threshold=-20dB" in chain
    assert "ratio=4.0" in chain


def test_true_peak_stays_below_full_scale():
    """The stream was measured clipping at +0.7 dBFS; the voice must not add to that."""
    assert _svc().true_peak < 0.0


def test_filter_is_a_single_valid_ffmpeg_chain():
    chain = _svc()._normalize_filter()
    assert "\n" not in chain and " " not in chain.strip()
    assert chain.count(",") == 1, "one comma separating exactly two filters"


@pytest.mark.parametrize("field,expected", [
    ("loudness_target", -12.0),
    ("true_peak", -1.5),
    ("compress_threshold", "-18dB"),
    ("compress_ratio", 3.0),
])
def test_defaults_match_the_measured_settings(field, expected):
    assert getattr(_svc(), field) == expected


def test_config_plumbs_loudness_through(monkeypatch, tmp_path):
    """A station.yaml override must actually reach the filter chain."""
    from bridge.config import TTSConfig
    cfg = TTSConfig(loudness_target=-9.5, true_peak=-1.0)
    svc = _svc(loudness_target=cfg.loudness_target, true_peak=cfg.true_peak)
    assert "I=-9.5" in svc._normalize_filter()
    assert "TP=-1.0" in svc._normalize_filter()


# =====================================================================
# HOURLY CHIME WORDING
# =====================================================================

def _say_text_default():
    for field in DongPlugin.config_fields():
        if field["key"] == "say_text":
            return field["default"]
    raise AssertionError("say_text field not found")


def test_chime_says_booong_not_dooong():
    """'dong' carries a second meaning listeners reach for every single time."""
    text = _say_text_default()
    assert "Booong" in text
    assert "Dooong" not in text.lower().replace("booong", "")
    assert "dooong" not in text.lower()


def test_chime_still_interpolates_the_time():
    text = _say_text_default()
    assert "{time}" in text
    assert text.format(time="14:00") == "Booong! The time is 14:00"


def test_schema_default_and_runtime_default_agree():
    """Two copies of the string exist; a rename must catch both."""
    import inspect
    src = inspect.getsource(DongPlugin.on_start)
    assert "Booong! The time is {time}" in src
    assert _say_text_default() == "Booong! The time is {time}"


def test_plugin_identity_unchanged():
    """Only the spoken word changed — renaming the plugin would orphan the
    stored `default-dong` instance."""
    assert DongPlugin.name == "dong"
