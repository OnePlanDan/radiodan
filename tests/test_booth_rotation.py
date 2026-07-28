"""Tests for booth log rotation.

Until 2026-07-28 the booth log was a plain FileHandler appending forever: 119
days and 471 776 lines of `HH:MM:SS`-only stamps in one 46.8 MB file, with no way
to tell which day any line belonged to. The June 2026 voice outage had to be
reconstructed from the event log and journald because the station's own
human-readable log couldn't answer "when".

Rotation and dating are one fix, not two: rotating without dates would just
produce a pile of undated files.
"""

import logging
import logging.handlers

import pytest

from bridge.booth import _LOG_RETENTION_DAYS, BoothLog, Event


@pytest.fixture
def booth_factory(tmp_path):
    """Fresh BoothLog instances with isolated logger names and handlers."""
    made: list[BoothLog] = []
    counter = {"n": 0}

    def build(console=False, **kwargs):
        counter["n"] += 1
        b = BoothLog(name=f"test.booth.{tmp_path.name}.{counter['n']}")
        b.configure(log_file=tmp_path / "booth.log", console=console, **kwargs)
        made.append(b)
        return b

    yield build

    for b in made:
        for h in list(b.logger.handlers):
            h.close()
            b.logger.removeHandler(h)


def _file_handler(booth):
    handlers = [h for h in booth.logger.handlers
                if isinstance(h, logging.handlers.TimedRotatingFileHandler)]
    assert len(handlers) == 1, f"expected one rotating file handler, got {handlers}"
    return handlers[0]


# =====================================================================
# HANDLER CONFIGURATION
# =====================================================================

def test_uses_a_rotating_handler_not_a_plain_one(booth_factory):
    booth = booth_factory()
    handler = _file_handler(booth)
    # A plain FileHandler is the regression; TimedRotatingFileHandler subclasses it.
    assert type(handler) is logging.handlers.TimedRotatingFileHandler


def test_rotates_daily_at_local_midnight(booth_factory):
    handler = _file_handler(booth_factory())
    assert handler.when == "MIDNIGHT"
    assert handler.interval == 24 * 60 * 60
    # Local, to match the formatter's local-time stamps: a file's name and its
    # contents must agree about what day it is.
    assert handler.utc is False


def test_rotated_files_are_named_by_date(booth_factory):
    """The whole point — HH:MM:SS lines are only readable if the file is one day."""
    handler = _file_handler(booth_factory())
    assert handler.suffix == "%Y-%m-%d"


def test_default_retention_is_thirty_days(booth_factory):
    assert _file_handler(booth_factory()).backupCount == _LOG_RETENTION_DAYS == 30


def test_retention_is_configurable(booth_factory):
    assert _file_handler(booth_factory(retention_days=7)).backupCount == 7


def test_encoding_is_explicit_utf8(booth_factory):
    """A systemd unit can inherit a locale where the platform default isn't UTF-8,
    and every line of this log carries emoji and box-drawing characters."""
    assert _file_handler(booth_factory()).encoding == "utf-8"


# =====================================================================
# BEHAVIOUR
# =====================================================================

def test_writes_go_to_the_log_file(booth_factory, tmp_path):
    booth = booth_factory()
    booth.track_change("Cypress Hill", "BANG OUT")
    _file_handler(booth).flush()

    content = (tmp_path / "booth.log").read_text(encoding="utf-8")
    assert "Cypress Hill" in content
    assert Event.TRACK_CHANGE.value in content


def test_unicode_survives_a_round_trip(booth_factory, tmp_path):
    booth = booth_factory()
    booth.error("Snön faller — TTS fallback: Eric → carlin")
    _file_handler(booth).flush()

    content = (tmp_path / "booth.log").read_text(encoding="utf-8")
    assert "Snön faller" in content
    assert "→" in content
    assert "│" in content, "the box separator must survive too"


def test_rollover_moves_todays_lines_into_a_dated_file(booth_factory, tmp_path):
    booth = booth_factory()
    handler = _file_handler(booth)

    booth.track_change("Ulf Lundell", "Snön faller")
    handler.flush()
    handler.doRollover()
    booth.track_change("Jay-Z", "Fallin'")
    handler.flush()

    dated = sorted(p.name for p in tmp_path.glob("booth.log.*"))
    assert len(dated) == 1, f"expected one dated file, got {dated}"
    # Name carries the date the lines inside it belong to.
    assert dated[0].startswith("booth.log.20")

    archived = (tmp_path / dated[0]).read_text(encoding="utf-8")
    current = (tmp_path / "booth.log").read_text(encoding="utf-8")
    assert "Ulf Lundell" in archived and "Jay-Z" not in archived
    assert "Jay-Z" in current and "Ulf Lundell" not in current


def _seed_old_days(tmp_path, days):
    for day in days:
        (tmp_path / f"booth.log.{day}").write_text("old\n", encoding="utf-8")


OLD_DAYS = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04", "2026-07-05"]


def test_retention_prunes_the_oldest_dated_files(booth_factory, tmp_path):
    """Same-day rollovers reuse one filename, so seed a week of dated files and
    let a real rollover do the pruning."""
    booth = booth_factory(retention_days=2)
    handler = _file_handler(booth)
    _seed_old_days(tmp_path, OLD_DAYS)

    booth.track_change("Artist", "Track")
    handler.flush()
    handler.doRollover()

    remaining = sorted(p.name for p in tmp_path.glob("booth.log.*"))
    assert len(remaining) == 2, f"backupCount must bound the dated files: {remaining}"
    assert "booth.log.2026-07-01" not in remaining, "the oldest must be gone"


def test_zero_retention_keeps_everything(booth_factory, tmp_path):
    """0 means no pruning — stdlib skips it entirely when backupCount is 0."""
    booth = booth_factory(retention_days=0)
    handler = _file_handler(booth)
    _seed_old_days(tmp_path, OLD_DAYS)

    booth.track_change("Artist", "Track")
    handler.flush()
    handler.doRollover()

    remaining = sorted(p.name for p in tmp_path.glob("booth.log.*"))
    assert all(f"booth.log.{d}" in remaining for d in OLD_DAYS), \
        f"history must survive: {remaining}"


def test_negative_retention_does_not_throw_history_away(booth_factory, tmp_path):
    """The clamp must land on 'keep everything', never on 'delete everything'."""
    booth = booth_factory(retention_days=-5)
    handler = _file_handler(booth)
    assert handler.backupCount == 0
    _seed_old_days(tmp_path, OLD_DAYS)

    booth.track_change("Artist", "Track")
    handler.flush()
    handler.doRollover()

    remaining = sorted(p.name for p in tmp_path.glob("booth.log.*"))
    assert all(f"booth.log.{d}" in remaining for d in OLD_DAYS), \
        f"history must survive: {remaining}"


def test_console_handler_still_added_when_requested(booth_factory):
    booth = booth_factory(console=True)
    streams = [h for h in booth.logger.handlers
               if type(h) is logging.StreamHandler]
    assert len(streams) == 1, "stdout still feeds journald"


def test_configure_is_idempotent(booth_factory, tmp_path):
    booth = booth_factory()
    before = len(booth.logger.handlers)
    booth.configure(log_file=tmp_path / "booth.log")
    assert len(booth.logger.handlers) == before, "no duplicate handlers, no double lines"


def test_lines_stay_time_only(booth_factory, tmp_path):
    """The date lives in the filename so the running sheet stays scannable."""
    import re
    booth = booth_factory()
    booth.track_change("GZA", "Investigative Reports")
    _file_handler(booth).flush()

    line = (tmp_path / "booth.log").read_text(encoding="utf-8").strip().splitlines()[0]
    assert re.match(r"^\d{2}:\d{2}:\d{2} ", line), line
    assert not re.match(r"^\d{4}-\d{2}-\d{2}", line), "no full date on the line"
