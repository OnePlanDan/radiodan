"""
Greeter — the station notices you arriving and is pleased about it.

Radio Dan usually plays to an empty room, so a listener connecting is the
single most important event the station can observe. This service watches the
Icecast listener count and reacts:

- Any arrival gets a spoken greeting over ducked music: welcome back, how long
  you were away, and one true fact about what happened while you were gone.
- The FIRST arrival of a day is the big one: the greeting breaks the current
  song and airs a freshly commissioned news bulletin — made today, for you.
- Independently of anyone listening, a bulletin is ordered every day at a
  configured hour, so there is usually a fresh one waiting. If a day's first
  listener beats the schedule, a catch-up order is placed on the spot and the
  bulletin airs the moment it lands.

Presence detection rides on ListenerTracker's Icecast reader but polls faster
(seconds, not the tracker's minute) because a greeting that arrives two songs
after you sat down is not a greeting.

The greeter never touches the music path on failure: TTS down, AudioSegment
down, or the database locked all degrade to "no greeting", never to silence.
"""

import asyncio
import logging
import random
import time
from datetime import datetime
from pathlib import Path

from bridge.audio.voice_scheduler import VoiceSegment

logger = logging.getLogger(__name__)

GREETER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS greeter_log (
    greeted_at REAL PRIMARY KEY,
    kind       TEXT NOT NULL,
    note       TEXT
);
CREATE INDEX IF NOT EXISTS idx_greeter_log_kind ON greeter_log(kind, greeted_at);
CREATE TABLE IF NOT EXISTS player_presence (
    name       TEXT NOT NULL,
    device     TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    PRIMARY KEY (name, device)
);
"""

# A presence heartbeat this fresh means that person is the one connecting now.
_PRESENCE_FRESH_SECONDS = 120.0

# greeter_log kinds
ARRIVAL = "arrival"            # a greeting went to air
FIRST_OF_DAY = "first_of_day"  # ...and it was the day's first
BULLETIN_AIR = "bulletin_air"  # a daily bulletin was put on the queue

# A sample of this arrival may already be in listener_samples by the time the
# gap is computed, so "when were you last here" ignores the most recent moments.
_GAP_EXCLUSION_SECONDS = 120.0

# Measured over 594 episodes: build ≈ 13 + 1.04 × length minutes. A short
# bulletin lands in about this long; quoted to the listener as an honest ETA.
_BULLETIN_BUILD_MINUTES = 20


def _local_midnight_ts() -> float:
    now = datetime.now()
    return datetime(now.year, now.month, now.day).timestamp()


def _humanize_gap(seconds: float) -> str:
    """A duration the way a person would say it on air."""
    minutes = seconds / 60
    if minutes < 2:
        return "barely a minute"
    if minutes < 50:
        return f"{round(minutes)} minutes"
    hours = minutes / 60
    if hours < 1.6:
        return "about an hour"
    if hours < 22:
        return f"about {round(hours)} hours"
    days = seconds / 86400
    if days < 1.5:
        return "about a day"
    return f"{round(days)} days"


def _daypart(hour: int) -> str:
    if hour < 5:
        return "night"
    if hour < 11:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "evening"


def build_greeting(
    *,
    listener_name: str = "",
    gap_seconds: float | None,
    first_of_day: bool,
    bulletin: dict,
    fun_fact: str | None,
    hour: int | None = None,
    device: str | None = None,
) -> str:
    """Compose the spoken greeting. Pure function so tests can read the words.

    `bulletin` is {"status": "ready"|"building"|"ordered"|"aired"|"none",
    "eta_minutes": int|None}. Output is speech: no markdown, no emoji.
    """
    hour = datetime.now().hour if hour is None else hour
    name = f" {listener_name}" if listener_name else ""
    part = _daypart(hour)

    if gap_seconds is None:
        openers = [
            f"Well hello there! Someone's listening — welcome to Radio Dan!",
            f"A listener! An actual listener! Welcome to Radio Dan — you've made my {part}.",
        ]
    else:
        openers = [
            f"Hey hey — look who's back! Good {part}{name}, welcome back to Radio Dan!",
            f"There you are{name}! Radio Dan just got its favourite listener back.",
            f"Well well, good {part}{name}! You're back — I'm genuinely delighted.",
        ]
    parts = [random.choice(openers)]

    if device:
        parts.append(f"On the {device} tonight, I see.")

    if gap_seconds is not None:
        gap = _humanize_gap(gap_seconds)
        days = gap_seconds / 86400
        if days >= 7:
            parts.append(
                f"It's been {gap} since you last tuned in — {round(days)} whole days. I noticed every one of them."
            )
        elif gap_seconds < 120:
            parts.append("You were here barely a minute ago, and I'm still glad to see you.")
        else:
            parts.append(f"It's been {gap} since you last tuned in.")

    if fun_fact:
        parts.append(fun_fact)

    if first_of_day:
        status = bulletin.get("status")
        eta = bulletin.get("eta_minutes")
        if status == "ready":
            parts.append(
                "And since it's your first visit today, I'm breaking the music: "
                "June Ferry's bulletin, made fresh today, coming up right now."
            )
        elif status in ("building", "ordered"):
            when = f"about {eta} minutes" if eta else "about twenty minutes"
            parts.append(
                f"Your first visit today, so I've got June Ferry writing today's bulletin "
                f"as we speak — it airs the moment it lands, in {when}. Music until then."
            )
        else:
            parts.append("First visit of the day — make yourself comfortable.")

    return " ".join(parts)


class GreeterService:
    """Watches for arrivals and makes them feel like arrivals."""

    def __init__(
        self,
        *,
        tracker,
        tts_service,
        mixer,
        voice_scheduler,
        planner,
        stream_context,
        db_path,
        commissions=None,
        stats=None,
        enabled: bool = True,
        listener_name: str = "",
        poll_interval: float = 10.0,
        cooldown_seconds: float = 180.0,
        boot_grace_seconds: float = 900.0,
        speaker: str | None = None,
        instruct: str | None = None,
        news_show: str = "",
        news_hour: int = 6,
        first_connect_episode: bool = True,
        location: str = "",
    ):
        self.tracker = tracker
        self.tts_service = tts_service
        self.mixer = mixer
        self.voice_scheduler = voice_scheduler
        self.planner = planner
        self.stream_context = stream_context
        self.db_path = db_path
        self.commissions = commissions
        self.stats = stats
        self.enabled = enabled
        self.listener_name = listener_name
        self.poll_interval = max(3.0, poll_interval)
        self.cooldown_seconds = cooldown_seconds
        self.boot_grace_seconds = boot_grace_seconds
        self.speaker = speaker or None
        self.instruct = instruct or None
        self.news_show = news_show
        self.news_hour = news_hour
        self.first_connect_episode = first_connect_episode
        self.location = location

        self._db = None
        self._task: asyncio.Task | None = None
        self._air_task: asyncio.Task | None = None
        self._last_listeners: int | None = None
        self._air_in_flight = False
        self.greetings_sent = 0

    # =====================================================================
    # LIFECYCLE
    # =====================================================================

    async def start(self) -> None:
        import aiosqlite

        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA busy_timeout = 5000")
        await self._db.executescript(GREETER_SCHEMA_SQL)
        await self._db.commit()
        if self.enabled:
            self._task = asyncio.create_task(self._run())
            logger.info(
                f"Greeter started (every {int(self.poll_interval)}s; daily bulletin "
                f"from '{self.news_show}' at {self.news_hour:02d}:00)"
                if self.news_show
                else f"Greeter started (every {int(self.poll_interval)}s; no news show)"
            )
        else:
            logger.info("Greeter disabled in config (state DB still open)")

    async def stop(self) -> None:
        for task in (self._task, self._air_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._task = None
        self._air_task = None
        if self._db:
            await self._db.close()
            self._db = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Greeting is decoration; the stream must never depend on it.
                logger.exception("Greeter tick failed")

    # =====================================================================
    # THE LOOP
    # =====================================================================

    async def tick(self) -> None:
        """One observation: order news if due, deliver a landed bulletin,
        detect an arrival."""
        await self._ensure_daily_news()

        reading = await self.tracker.read_icecast()
        if reading is None:
            return
        listeners, _peak = reading
        prev, self._last_listeners = self._last_listeners, listeners

        if listeners > 0:
            await self._deliver_if_landed()

        arrived = prev == 0 and listeners > 0
        if prev is None and listeners > 0:
            # First reading after a restart with someone already connected.
            # Greet only if we haven't greeted recently — a bridge restart
            # mid-listen must not re-greet the same session.
            last = await self._last_greeting_at()
            arrived = last is None or (time.time() - last) > self.boot_grace_seconds

        if arrived:
            await self.greet()

    # =====================================================================
    # GREETING
    # =====================================================================

    async def greet(self, *, force: bool = False, force_first_of_day: bool = False) -> dict:
        """Greet the listener. Returns what happened (for the API and tests)."""
        now = time.time()
        last = await self._last_greeting_at()
        if not force and last is not None and (now - last) < self.cooldown_seconds:
            logger.info(f"Arrival within cooldown ({int(now - last)}s ago) — staying quiet")
            return {"greeted": False, "reason": "cooldown"}

        gap = await self._gap_before_now()
        first = force_first_of_day or await self._first_of_day_pending()
        bulletin = {"status": "none", "eta_minutes": None}
        if first and self.first_connect_episode:
            bulletin = await self._bulletin_state(order_if_missing=True)
        fact = await self._fun_fact(gap)

        # The player page identifies its listener; a fresh heartbeat beats the
        # configured default name, and we get to mention the device.
        name, device = self.listener_name, None
        who = await self.freshest_presence()
        if who:
            name, device = who["name"], (who["device"] or None)

        text = build_greeting(
            listener_name=name,
            gap_seconds=gap,
            first_of_day=first,
            bulletin=bulletin,
            fun_fact=fact,
            device=device,
        )

        try:
            audio = await self.tts_service.speak(text, speaker=self.speaker, instruct=self.instruct)
            duration = await _audio_duration(audio)
        except Exception:
            logger.exception("Greeting TTS failed — arrival goes unmarked")
            return {"greeted": False, "reason": "tts_failed"}

        # Interrupt priority: a greeting must not queue politely behind DJ chatter.
        await self.voice_scheduler.submit(VoiceSegment(
            text=text,
            trigger="asap",
            priority=-5,
            source_plugin="greeter",
            pre_generated_audio=Path(audio),
            audio_duration=duration,
            speaker=self.speaker,
            instruct=self.instruct,
        ))
        await self._push_icy_text(self._icy_headline(gap, first))

        kind = FIRST_OF_DAY if first else ARRIVAL
        await self._log(kind, text[:200])
        self.greetings_sent += 1
        logger.info(f"Greeted listener ({kind}, gap={gap and round(gap)}s): {text[:90]}")

        aired = False
        if first and bulletin.get("status") == "ready":
            self._schedule_bulletin_break(bulletin["row"], wait=duration + 1.5)
            aired = True

        return {
            "greeted": True, "kind": kind, "text": text,
            "gap_seconds": gap, "bulletin": bulletin.get("status"),
            "bulletin_airing": aired, "voice_seconds": duration,
        }

    def _icy_headline(self, gap: float | None, first: bool) -> str:
        if first:
            return "Welcome back! Your daily bulletin is on its way"
        if gap and gap > 3600:
            return f"Welcome back! It's been {_humanize_gap(gap)}"
        return "Welcome back to Radio Dan!"

    async def _push_icy_text(self, headline: str) -> None:
        """Show the greeting in the player's now-playing line (StreamTitle)."""
        # Comma is the pair separator in music.set_metadata; newline ends telnet.
        clean = headline.replace(",", " —").replace("\n", " ")
        try:
            await self.mixer._send_command(f"music.set_metadata artist=Radio Dan,title={clean}")
        except Exception:
            logger.debug("Could not push greeting to ICY metadata")

    # =====================================================================
    # THE DAILY BULLETIN
    # =====================================================================

    async def _ensure_daily_news(self) -> None:
        """Order today's bulletin at the configured hour, listener or not."""
        if not self._news_possible():
            return
        if datetime.now().hour < self.news_hour:
            return
        row = await self.commissions.latest_for_show(
            self.news_show, since_ts=_local_midnight_ts()
        )
        if row is not None:
            return
        await self._order_bulletin(reason="scheduled")

    async def _bulletin_state(self, order_if_missing: bool = False) -> dict:
        """Where today's bulletin stands, optionally ordering a catch-up."""
        if not self._news_possible():
            return {"status": "none", "eta_minutes": None}

        row = await self.commissions.latest_for_show(
            self.news_show, since_ts=_local_midnight_ts()
        )
        if row is None and order_if_missing:
            row = await self._order_bulletin(reason="first listener of the day")
            if row is not None:
                return {"status": "ordered", "eta_minutes": _BULLETIN_BUILD_MINUTES, "row": row}

        if row is None:
            return {"status": "none", "eta_minutes": None}

        state = row["state"]
        if state == "ready":
            return {"status": "ready", "eta_minutes": 0, "row": row}
        if state == "pending":
            eta = max(1, round((row["requested_at"] + _BULLETIN_BUILD_MINUTES * 60 - time.time()) / 60))
            return {"status": "building", "eta_minutes": eta, "row": row}
        if state == "aired":
            return {"status": "aired", "eta_minutes": None, "row": row}
        return {"status": "none", "eta_minutes": None}

    def _news_possible(self) -> bool:
        return (
            self.commissions is not None
            and bool(self.news_show)
            and self.news_show in getattr(self.commissions, "owned_shows", set())
        )

    async def _order_bulletin(self, reason: str):
        when = datetime.now()
        concept = (
            f"The daily bulletin for {when.strftime('%A %d %B %Y')}. Real, current news: "
            f"the three or four stories that actually matter today — the world, Sweden, "
            f"and Gothenburg — with the weather near the end. This edition airs in the "
            f"{_daypart(when.hour)}, so greet the day as it stands; do not call it morning "
            f"if it is not. Keep it tight: this is the day's news, not a retrospective."
        )
        try:
            row = await self.commissions.commission(
                self.news_show, concept, location=self.location or None
            )
            logger.info(f"Ordered today's bulletin ({reason}): job {row['job_id'][:8]}")
            return row
        except Exception:
            logger.exception("Could not order today's bulletin")
            return None

    def _schedule_bulletin_break(self, row, wait: float) -> None:
        """After the greeting finishes: break the song, air the bulletin."""
        if self._air_in_flight:
            return
        self._air_in_flight = True
        job_id = row["job_id"]

        async def _break():
            try:
                await asyncio.sleep(wait)
                item = self.commissions.to_queue_item(row)
                if not await self.planner.insert_item(item, 0):
                    logger.error("Planner refused the bulletin — leaving it ready")
                    return
                await self.mixer.next_track()
                # A system skip: the DJ must not react as if the listener
                # rejected a song, or its reaction track races the bulletin.
                await self.stream_context.notify_skip(source="system")
                await self.commissions.mark_aired(job_id)
                await self._log(BULLETIN_AIR, f"{item.get('title')} ({job_id[:8]})")
                logger.info(f"Broke the song for today's bulletin: {item.get('title')}")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Bulletin break failed")
            finally:
                self._air_in_flight = False

        self._air_task = asyncio.create_task(_break())

    async def _deliver_if_landed(self) -> None:
        """A bulletin that finished building while someone was already listening
        goes on air next, with a spoken heads-up — no skip 20 minutes in."""
        if self._air_in_flight or not self._news_possible():
            return
        if not await self._first_of_day_done():
            return  # the arrival path owns the day's first play
        if await self._bulletin_aired_today():
            return
        state = await self._bulletin_state()
        if state["status"] != "ready":
            return

        row = state["row"]
        self._air_in_flight = True
        try:
            item = self.commissions.to_queue_item(row)
            if not await self.planner.insert_item(item, 0):
                logger.error("Planner refused the landed bulletin")
                return
            await self.commissions.mark_aired(row["job_id"])
            await self._log(BULLETIN_AIR, f"{item.get('title')} ({row['job_id'][:8]})")
            text = (
                "Hot off the press — June Ferry's bulletin for today just landed. "
                "It's up next, right after this song."
            )
            audio = await self.tts_service.speak(text, speaker=self.speaker, instruct=self.instruct)
            await self.voice_scheduler.submit(VoiceSegment(
                text=text, trigger="asap", source_plugin="greeter",
                pre_generated_audio=Path(audio),
                audio_duration=await _audio_duration(audio),
                speaker=self.speaker, instruct=self.instruct,
            ))
            logger.info(f"Landed bulletin queued next: {item.get('title')}")
        except Exception:
            logger.exception("Delivering the landed bulletin failed")
        finally:
            self._air_in_flight = False

    # =====================================================================
    # STATE QUESTIONS
    # =====================================================================

    async def _last_greeting_at(self) -> float | None:
        async with self._db.execute(
            "SELECT MAX(greeted_at) FROM greeter_log WHERE kind IN (?, ?)",
            (ARRIVAL, FIRST_OF_DAY),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    async def _first_of_day_pending(self) -> bool:
        return not await self._first_of_day_done()

    async def _first_of_day_done(self) -> bool:
        async with self._db.execute(
            "SELECT 1 FROM greeter_log WHERE kind = ? AND greeted_at >= ? LIMIT 1",
            (FIRST_OF_DAY, _local_midnight_ts()),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _bulletin_aired_today(self) -> bool:
        async with self._db.execute(
            "SELECT 1 FROM greeter_log WHERE kind = ? AND greeted_at >= ? LIMIT 1",
            (BULLETIN_AIR, _local_midnight_ts()),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def _gap_before_now(self) -> float | None:
        """Seconds since the listener was last heard, before this arrival."""
        cutoff = time.time() - _GAP_EXCLUSION_SECONDS
        try:
            async with self._db.execute(
                "SELECT MAX(sampled_at) FROM listener_samples "
                "WHERE listeners > 0 AND sampled_at < ?",
                (cutoff,),
            ) as cursor:
                row = await cursor.fetchone()
        except Exception:
            # The tracker owns that table; a station without it just has no history.
            return None
        if not row or not row[0]:
            return None
        return time.time() - row[0]

    async def _fun_fact(self, gap: float | None) -> str | None:
        """One true, spoken statistic. The station loves its numbers."""
        if self.stats is None:
            return None
        try:
            snap = await self.stats.snapshot()
        except Exception:
            logger.debug("Stats snapshot failed — greeting goes out factless")
            return None

        facts: list[str] = []
        if gap and gap > 600:
            played = await self.stats.songs_since(time.time() - gap)
            if played and played > 2:
                facts.append(
                    f"While you were away I played {played} songs to an empty room — "
                    f"somebody had to hear them."
                )
        plays = snap.get("songs_played_today")
        if plays:
            facts.append(f"You're joining song number {plays + 1} of today's broadcast.")
        disk = snap.get("disk_free_gb")
        if disk is not None:
            facts.append(f"Station health report: {disk} gigabytes of disk to spare.")
        tracks = snap.get("library_tracks")
        if tracks:
            facts.append(f"The record library stands at {tracks} tracks and counting.")
        bulletins = snap.get("bulletins_aired")
        if bulletins:
            facts.append(f"June Ferry has filed {bulletins} bulletins for this station so far.")
        up = snap.get("uptime_hours")
        if up and up >= 2:
            span = f"{round(up / 24)} days" if up >= 48 else f"{round(up)} hours"
            facts.append(f"I've been on the air {span} without a break.")

        return random.choice(facts) if facts else None

    async def _log(self, kind: str, note: str) -> None:
        await self._db.execute(
            "INSERT OR REPLACE INTO greeter_log (greeted_at, kind, note) VALUES (?, ?, ?)",
            (time.time(), kind, note),
        )
        await self._db.commit()

    # =====================================================================
    # NAMED PRESENCE (from the player page)
    # =====================================================================

    async def note_presence(self, name: str, device: str = "") -> None:
        """The player page says who is listening. Icecast counts; this names."""
        now = time.time()
        await self._db.execute(
            "INSERT INTO player_presence (name, device, first_seen, last_seen) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(name, device) DO UPDATE SET last_seen = excluded.last_seen",
            (name.strip()[:40], device.strip()[:40], now, now),
        )
        await self._db.commit()

    async def freshest_presence(self, window: float = _PRESENCE_FRESH_SECONDS):
        """Who most recently identified themselves, if anyone did just now."""
        async with self._db.execute(
            "SELECT name, device, last_seen FROM player_presence "
            "WHERE last_seen >= ? ORDER BY last_seen DESC LIMIT 1",
            (time.time() - window,),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def known_listeners(self, limit: int = 20) -> list:
        async with self._db.execute(
            "SELECT name, device, first_seen, last_seen FROM player_presence "
            "ORDER BY last_seen DESC LIMIT ?",
            (limit,),
        ) as cursor:
            return [dict(row) async for row in cursor]

    async def recent(self, limit: int = 10) -> list:
        async with self._db.execute(
            "SELECT greeted_at, kind, note FROM greeter_log "
            "ORDER BY greeted_at DESC LIMIT ?",
            (limit,),
        ) as cursor:
            return [dict(row) async for row in cursor]


async def _audio_duration(path) -> float:
    """Voice length in seconds via ffprobe — needed to time the song break."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "csv=p=0", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        return float(stdout.decode().strip())
    except Exception:
        logger.warning(f"Could not measure greeting duration for {path}; assuming 12s")
        return 12.0
