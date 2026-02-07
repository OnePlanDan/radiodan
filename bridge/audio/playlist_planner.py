"""
RadioDan Playlist Planner

Plans songs ahead, enabling pre-emptive TTS generation and precise
bridge timing across crossfades.

Architecture:
  PlaylistPlanner maintains a lookahead queue of upcoming tracks.
  When Liquidsoap plays a track, StreamContext detects it and calls
  planner.advance() which shifts the queue, fills it, pushes new
  tracks to Liquidsoap, and emits "tts_needed" for the N+2 position.

  MusicLibraryScanner reads ID3 tags (via mutagen) with folder/filename
  fallback and stores results in SQLite.

  SelectionStrategy is a protocol for pluggable track selection.
  Feeder plugins (e.g. SimplePlaylistFeeder) implement this protocol
  and register themselves via set_feeder() at startup.
"""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Protocol, runtime_checkable

import aiosqlite

from bridge.booth import booth

from typing import TYPE_CHECKING as _TC

if _TC:
    from bridge.event_store import EventStore

logger = logging.getLogger(__name__)

# Type alias for async event callbacks
EventCallback = Callable[..., Coroutine[Any, Any, None]]

# Supported audio extensions for library scanning
AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".wav", ".m4a", ".aac", ".opus", ".wma"}

# SQL schema for playlist tables (lives alongside config_store in radiodan.db)
PLAYLIST_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS music_library (
    file_path TEXT PRIMARY KEY,
    artist TEXT,
    title TEXT,
    album TEXT,
    genre TEXT,
    year TEXT,
    duration_seconds REAL,
    file_hash TEXT,
    last_scanned TEXT
);

CREATE TABLE IF NOT EXISTS playlist_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    played_at TEXT NOT NULL,
    planned_position INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS playlist_queue (
    position INTEGER PRIMARY KEY,
    file_path TEXT NOT NULL,
    metadata TEXT DEFAULT '{}',
    tts_status TEXT DEFAULT 'pending',
    tts_path TEXT
);
"""


# =========================================================================
# SELECTION STRATEGY
# =========================================================================

@runtime_checkable
class SelectionStrategy(Protocol):
    """Protocol for pluggable track selection algorithms."""

    async def select_next(
        self,
        library: list[dict],
        history: list[dict],
        upcoming: list[dict],
    ) -> dict | None:
        """Select the next track to add to the queue.

        Args:
            library: All known tracks in the music library
            history: Recently played tracks (newest first)
            upcoming: Currently queued upcoming tracks

        Returns:
            A track dict from library, or None if no suitable track found
        """
        ...


# =========================================================================
# MUSIC LIBRARY SCANNER
# =========================================================================

class MusicLibraryScanner:
    """Scans a directory for audio files and reads ID3 metadata."""

    def __init__(self, music_dir: Path):
        self.music_dir = music_dir

    async def scan(self) -> list[dict]:
        """Scan the music directory for audio files.

        Returns a list of track dicts with metadata read from ID3 tags,
        falling back to folder/filename parsing.
        """
        tracks = []
        if not self.music_dir.exists():
            logger.warning(f"Music directory not found: {self.music_dir}")
            return tracks

        # Run file I/O in executor to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        files = await loop.run_in_executor(None, self._find_audio_files)

        for file_path in files:
            try:
                track = await loop.run_in_executor(None, self._read_track, file_path)
                if track:
                    tracks.append(track)
            except Exception:
                logger.warning(f"Failed to read metadata: {file_path}", exc_info=True)

        logger.info(f"Library scan complete: {len(tracks)} tracks found in {self.music_dir}")
        return tracks

    def _find_audio_files(self) -> list[Path]:
        """Find all audio files recursively."""
        files = []
        for ext in AUDIO_EXTENSIONS:
            files.extend(self.music_dir.rglob(f"*{ext}"))
        return sorted(files)

    def _read_track(self, file_path: Path) -> dict | None:
        """Read metadata from a single audio file."""
        try:
            from mutagen import File as MutagenFile
        except ImportError:
            logger.error("mutagen not installed — cannot read ID3 tags")
            return self._fallback_metadata(file_path)

        audio = MutagenFile(file_path, easy=True)

        # Extract ID3 tags if available
        artist = ""
        title = ""
        album = ""
        genre = ""
        year = ""
        duration = 0.0

        if audio is not None:
            duration = audio.info.length if audio.info else 0.0
            if audio.tags:
                artist = _first_tag(audio.tags, "artist")
                title = _first_tag(audio.tags, "title")
                album = _first_tag(audio.tags, "album")
                genre = _first_tag(audio.tags, "genre")
                year = _first_tag(audio.tags, "date") or _first_tag(audio.tags, "year")

        # Fallback: parse from folder/filename
        if not artist or not title:
            fb = self._parse_path(file_path)
            artist = artist or fb.get("artist", "")
            title = title or fb.get("title", "")

        # Compute file hash for change detection
        file_hash = self._quick_hash(file_path)

        return {
            "file_path": str(file_path),
            "artist": artist,
            "title": title,
            "album": album,
            "genre": genre,
            "year": year,
            "duration_seconds": duration,
            "file_hash": file_hash,
            "last_scanned": datetime.now(timezone.utc).isoformat(),
        }

    def _fallback_metadata(self, file_path: Path) -> dict:
        """Minimal metadata when mutagen is unavailable."""
        fb = self._parse_path(file_path)
        return {
            "file_path": str(file_path),
            "artist": fb.get("artist", ""),
            "title": fb.get("title", ""),
            "album": "",
            "genre": "",
            "year": "",
            "duration_seconds": 0.0,
            "file_hash": self._quick_hash(file_path),
            "last_scanned": datetime.now(timezone.utc).isoformat(),
        }

    def _parse_path(self, file_path: Path) -> dict:
        """Parse artist/title from path structure.

        Tries these patterns:
          .../Artist/Album/01 - Title.mp3 → artist=Artist, title=Title
          .../Artist/Title.mp3             → artist=Artist, title=Title
          .../Artist - Title.mp3           → artist=Artist, title=Title
          .../Title.mp3                    → artist="", title=Title
        """
        stem = file_path.stem
        parts = file_path.relative_to(self.music_dir).parts if file_path.is_relative_to(self.music_dir) else ()

        # Try "Artist - Title" in filename
        if " - " in stem:
            artist, _, title = stem.partition(" - ")
            # Strip leading track numbers (e.g. "01 - Title")
            if artist.strip().isdigit() and len(parts) >= 2:
                artist = parts[-2] if len(parts) >= 2 else ""
                title = title.strip()
            return {"artist": artist.strip(), "title": title.strip()}

        # Strip leading track numbers from stem
        clean_stem = stem.lstrip("0123456789.- ").strip() or stem

        # Try parent directory as artist
        if len(parts) >= 2:
            return {"artist": parts[-2], "title": clean_stem}

        return {"artist": "", "title": clean_stem}

    @staticmethod
    def _quick_hash(file_path: Path) -> str:
        """Quick hash of first 8KB + file size for change detection."""
        h = hashlib.md5()
        try:
            size = file_path.stat().st_size
            h.update(str(size).encode())
            with open(file_path, "rb") as f:
                h.update(f.read(8192))
        except OSError:
            pass
        return h.hexdigest()


def _first_tag(tags: dict, key: str) -> str:
    """Extract first value from a mutagen tag list."""
    val = tags.get(key)
    if val:
        if isinstance(val, list):
            return str(val[0]).strip() if val else ""
        return str(val).strip()
    return ""


# =========================================================================
# PLAYLIST PLANNER
# =========================================================================

class PlaylistPlanner:
    """
    Core playlist planning service.

    Maintains a lookahead queue of upcoming tracks, pushing them to
    Liquidsoap's request queue. Emits events when the queue shifts
    and when tracks need TTS pre-generation.

    Events:
      "queue_changed"    — queue shifted (args: upcoming list)
      "library_scanned"  — scan complete (args: track count)
      "tts_needed"       — track entered N+2 position (args: track dict, position)
    """

    def __init__(
        self,
        mixer: Any,
        db_path: Path,
        music_dir: Path,
        lookahead: int = 5,
        scan_interval: float = 300.0,
        crossfade_duration: float = 5.0,
    ):
        self.mixer = mixer
        self.db_path = db_path
        self.music_dir = music_dir
        self.lookahead = lookahead
        self.scan_interval = scan_interval
        self.crossfade_duration = crossfade_duration

        self._strategy: SelectionStrategy | None = None
        self._no_feeder_warned = False
        self._scanner = MusicLibraryScanner(music_dir)

        # In-memory state
        self._library: list[dict] = []
        self._upcoming: list[dict] = []
        self._history: list[dict] = []

        # Event subscribers
        self._listeners: dict[str, list[EventCallback]] = {}

        # Database connection
        self._db: aiosqlite.Connection | None = None

        # Background tasks
        self._scan_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

        # Event store for timeline (optional)
        self._event_store: "EventStore | None" = None

    def set_event_store(self, event_store: "EventStore") -> None:
        """Set the event store for timeline instrumentation."""
        self._event_store = event_store

    # =====================================================================
    # PROPERTIES
    # =====================================================================

    @property
    def upcoming(self) -> list[dict]:
        """Read-only copy of the upcoming queue."""
        return list(self._upcoming)

    @property
    def library(self) -> list[dict]:
        """All known tracks in the music library."""
        return list(self._library)

    # =====================================================================
    # FEEDER REGISTRATION
    # =====================================================================

    def set_feeder(self, strategy: SelectionStrategy) -> None:
        """Register a feeder plugin as the track selection strategy.

        Triggers an immediate queue fill once a feeder is available.
        """
        if self._strategy is not None:
            logger.warning(
                f"Replacing feeder: {type(self._strategy).__name__} -> {type(strategy).__name__}"
            )
        self._strategy = strategy
        self._no_feeder_warned = False
        logger.info(f"Feeder set: {type(strategy).__name__}")
        # Auto-fill queue now that we have a feeder
        asyncio.create_task(self._deferred_fill())

    def clear_feeder(self) -> None:
        """Unregister the current feeder plugin."""
        if self._strategy is not None:
            logger.info(f"Feeder cleared: {type(self._strategy).__name__}")
            self._strategy = None

    async def _deferred_fill(self) -> None:
        """Fill queue and push to Liquidsoap after a feeder registers.

        Retries with backoff to handle the startup race condition where
        the bot starts before Liquidsoap's telnet is fully ready.
        Verifies pushes by checking Liquidsoap's actual queue length.
        """
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                async with self._lock:
                    added = await self._fill_queue_unlocked()

                    if added:
                        await self._save_queue_to_db()
                        await self._emit("queue_changed", self._upcoming)
                        self._publish_upcoming()

                    # Push all queued tracks (re-push handles startup failures)
                    await self._push_all_to_liquidsoap()

                    total = len(self._upcoming)

                # Verify by checking Liquidsoap's actual queue length
                ls_count = await self.mixer.get_music_queue_length()
                logger.info(
                    f"Deferred fill (attempt {attempt + 1}): "
                    f"{len(added)} new, {total} in Python queue, "
                    f"{ls_count} confirmed in Liquidsoap"
                )

                if ls_count > 0 or total == 0:
                    return  # Liquidsoap has tracks, we're good

                # Liquidsoap queue is empty despite pushes — not ready yet
                delay = 2 * (attempt + 1)
                logger.warning(
                    f"Liquidsoap queue empty after pushing {total} tracks, "
                    f"retrying in {delay}s..."
                )
                await asyncio.sleep(delay)
            except Exception:
                logger.exception("Failed to fill queue after feeder registration")
                return

    # =====================================================================
    # EVENTS
    # =====================================================================

    def on(self, event: str, callback: EventCallback) -> None:
        """Subscribe to a planner event."""
        self._listeners.setdefault(event, []).append(callback)

    async def _emit(self, event: str, *args: Any, **kwargs: Any) -> None:
        """Emit an event to all subscribers."""
        for callback in self._listeners.get(event, []):
            try:
                await callback(*args, **kwargs)
            except Exception:
                logger.exception(f"Error in {event} listener {callback.__qualname__}")

    # =====================================================================
    # LIFECYCLE
    # =====================================================================

    async def start(self) -> None:
        """Open DB, scan library, push persisted queue to Liquidsoap.

        Queue filling is deferred until a feeder plugin registers via set_feeder().
        """
        # Open database
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(PLAYLIST_SCHEMA_SQL)
        await self._db.commit()

        # Load library from DB cache first (fast startup)
        self._library = await self._load_library_from_db()

        # Load any persisted queue
        self._upcoming = await self._load_queue_from_db()

        # Load recent history
        self._history = await self.get_history(limit=50)

        # Initial scan (updates DB)
        await self._scan_library()

        # Push persisted queue tracks to Liquidsoap (no fill — that waits for feeder)
        await self._push_all_to_liquidsoap()

        # Start periodic rescan
        if self.scan_interval > 0:
            self._scan_task = asyncio.create_task(self._scan_loop())

        track_count = len(self._library)
        queue_count = len(self._upcoming)
        booth.start(f"Playlist planner ({track_count} tracks, {queue_count} queued)")
        logger.info(f"Playlist planner started: {track_count} tracks, {queue_count} queued")

    async def stop(self) -> None:
        """Cancel tasks, persist queue, close DB."""
        if self._scan_task:
            self._scan_task.cancel()
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
            self._scan_task = None

        # Persist current queue state
        if self._db:
            await self._save_queue_to_db()
            await self._db.close()
            self._db = None

        booth.stop("Playlist planner")
        logger.info("Playlist planner stopped")

    # =====================================================================
    # ADVANCE (called on track_changed)
    # =====================================================================

    async def advance(self, track_info: dict) -> None:
        """Called by StreamContext when a new track starts playing.

        1. Record play in history
        2. Shift queue (pop position 0 if it matches)
        3. Fill queue with new track(s)
        4. Push new track to Liquidsoap
        5. Emit tts_needed for track at position 1 (= N+2)
        """
        async with self._lock:
            current_filename = track_info.get("filename", "")

            # Record in history
            await self._record_history(current_filename)

            # Shift queue: remove the track that just started playing
            if self._upcoming and self._is_same_track(self._upcoming[0], current_filename):
                self._upcoming.pop(0)
            elif self._upcoming:
                # Track playing doesn't match queue head — might be fallback playlist
                # Try to find and remove it from elsewhere in the queue
                for i, t in enumerate(self._upcoming):
                    if self._is_same_track(t, current_filename):
                        self._upcoming.pop(i)
                        break

            # Fill queue back up
            added = await self._fill_queue_unlocked()

            # Push any newly added tracks to Liquidsoap
            for track in added:
                await self._push_track_to_liquidsoap(track)

            # Persist queue
            await self._save_queue_to_db()

            # Emit queue_changed
            await self._emit("queue_changed", self._upcoming)
            self._publish_upcoming()

            # Emit tts_needed for N+2 position (index 1 in the 0-based upcoming list)
            if len(self._upcoming) > 1:
                tts_track = self._upcoming[1]
                await self._emit("tts_needed", tts_track, 1)
                logger.info(
                    f"TTS needed for upcoming: {tts_track.get('artist', '?')} - {tts_track.get('title', '?')}"
                )

    def _is_same_track(self, track: dict, filename: str) -> bool:
        """Check if a queued track matches a Liquidsoap filename."""
        track_path = track.get("file_path", "")
        # Liquidsoap reports container paths; compare basenames
        return Path(track_path).name == Path(filename).name

    # =====================================================================
    # HISTORY
    # =====================================================================

    async def get_history(self, limit: int = 20) -> list[dict]:
        """Get recent play history."""
        if not self._db:
            return []

        rows = []
        async with self._db.execute(
            "SELECT file_path, played_at, planned_position FROM playlist_history "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cursor:
            async for row in cursor:
                rows.append({
                    "file_path": row["file_path"],
                    "played_at": row["played_at"],
                    "planned_position": row["planned_position"],
                })
        return rows

    async def _record_history(self, filename: str) -> None:
        """Record a track play in history."""
        if not self._db or not filename:
            return

        # Find full path from library
        file_path = filename
        for track in self._library:
            if Path(track["file_path"]).name == Path(filename).name:
                file_path = track["file_path"]
                break

        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO playlist_history (file_path, played_at) VALUES (?, ?)",
            (file_path, now),
        )
        await self._db.commit()

        # Update in-memory history
        self._history.insert(0, {"file_path": file_path, "played_at": now})
        self._history = self._history[:50]  # Keep bounded

    def _serialize_upcoming(self) -> list[dict]:
        """Serialize the upcoming queue for SSE broadcast."""
        return [
            {
                "artist": t.get("artist", ""),
                "title": t.get("title", ""),
                "duration_seconds": t.get("duration_seconds", 0),
                "file_path": t.get("file_path", ""),
            }
            for t in self._upcoming
        ]

    def _publish_upcoming(self) -> None:
        """Publish upcoming queue to event_store subscribers (non-blocking)."""
        if self._event_store:
            self._event_store._publish({
                "action": "queue_changed",
                "upcoming": self._serialize_upcoming(),
            })

    # =====================================================================
    # QUEUE MANAGEMENT
    # =====================================================================

    async def _fill_queue(self) -> list[dict]:
        """Fill queue to lookahead depth. Returns newly added tracks."""
        async with self._lock:
            return await self._fill_queue_unlocked()

    async def _fill_queue_unlocked(self) -> list[dict]:
        """Fill queue without acquiring lock (caller must hold lock)."""
        if self._strategy is None:
            if self._library and not self._no_feeder_warned:
                logger.warning("No feeder plugin active — queue will not be filled")
                self._no_feeder_warned = True
            return []

        added = []
        while len(self._upcoming) < self.lookahead:
            track = await self._strategy.select_next(
                self._library, self._history, self._upcoming
            )
            if track is None:
                break
            self._upcoming.append(track)
            added.append(track)

        return added

    async def _push_all_to_liquidsoap(self) -> None:
        """Push all queued tracks to Liquidsoap's music_q."""
        for track in self._upcoming:
            await self._push_track_to_liquidsoap(track)

    async def _push_track_to_liquidsoap(self, track: dict) -> None:
        """Push a single track to Liquidsoap's music_q."""
        file_path = Path(track["file_path"])
        try:
            success = await self.mixer.queue_music(file_path)
            if success:
                artist = track.get("artist", "?")
                title = track.get("title", "?")
                logger.debug(f"Pushed to Liquidsoap: {artist} - {title}")
            else:
                logger.warning(f"Failed to push track: {track['file_path']}")
        except Exception:
            logger.exception(f"Error pushing track to Liquidsoap: {track['file_path']}")

    # =====================================================================
    # DB PERSISTENCE
    # =====================================================================

    async def _save_queue_to_db(self) -> None:
        """Persist the current queue state to SQLite."""
        if not self._db:
            return
        await self._db.execute("DELETE FROM playlist_queue")
        for i, track in enumerate(self._upcoming):
            metadata = json.dumps({
                k: v for k, v in track.items()
                if k not in ("file_path",)
            })
            await self._db.execute(
                "INSERT INTO playlist_queue (position, file_path, metadata) VALUES (?, ?, ?)",
                (i, track["file_path"], metadata),
            )
        await self._db.commit()

    async def _load_queue_from_db(self) -> list[dict]:
        """Load persisted queue from SQLite."""
        if not self._db:
            return []
        tracks = []
        async with self._db.execute(
            "SELECT file_path, metadata, tts_status, tts_path "
            "FROM playlist_queue ORDER BY position"
        ) as cursor:
            async for row in cursor:
                track = json.loads(row["metadata"])
                track["file_path"] = row["file_path"]
                track["tts_status"] = row["tts_status"]
                track["tts_path"] = row["tts_path"]
                tracks.append(track)
        return tracks

    # =====================================================================
    # LIBRARY SCANNING
    # =====================================================================

    async def _scan_library(self) -> None:
        """Scan music directory and update the library."""
        scanned = await self._scanner.scan()
        if not scanned:
            if not self._library:
                logger.warning("No tracks found in music directory")
            return

        # Update DB
        if self._db:
            for track in scanned:
                await self._db.execute(
                    """INSERT OR REPLACE INTO music_library
                    (file_path, artist, title, album, genre, year,
                     duration_seconds, file_hash, last_scanned)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        track["file_path"],
                        track["artist"],
                        track["title"],
                        track["album"],
                        track["genre"],
                        track["year"],
                        track["duration_seconds"],
                        track["file_hash"],
                        track["last_scanned"],
                    ),
                )
            await self._db.commit()

        self._library = scanned
        await self._emit("library_scanned", len(scanned))

    async def _load_library_from_db(self) -> list[dict]:
        """Load cached library from SQLite for fast startup."""
        if not self._db:
            return []
        tracks = []
        async with self._db.execute(
            "SELECT file_path, artist, title, album, genre, year, "
            "duration_seconds, file_hash, last_scanned FROM music_library"
        ) as cursor:
            async for row in cursor:
                tracks.append({
                    "file_path": row["file_path"],
                    "artist": row["artist"],
                    "title": row["title"],
                    "album": row["album"],
                    "genre": row["genre"],
                    "year": row["year"],
                    "duration_seconds": row["duration_seconds"],
                    "file_hash": row["file_hash"],
                    "last_scanned": row["last_scanned"],
                })
        return tracks

    async def _scan_loop(self) -> None:
        """Periodically rescan the music library."""
        while True:
            await asyncio.sleep(self.scan_interval)
            try:
                await self._scan_library()
                logger.info(f"Library rescan complete: {len(self._library)} tracks")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Library rescan failed")
