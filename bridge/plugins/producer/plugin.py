"""ProducerPlugin — seed-driven DJ with YAML-defined characters."""

from __future__ import annotations

import asyncio
import logging
import random
from pathlib import Path
from typing import Any

from bridge.plugins import register_plugin
from bridge.plugins.base import DJPlugin
from bridge.plugins.producer.characters import load_characters
from bridge.plugins.producer.context_providers import gather_context, select_songs
from bridge.plugins.producer.models import CharacterConfig, Script, ScriptSegment, SeedState
from bridge.plugins.producer.script_executor import ScriptExecutor
from bridge.plugins.producer.script_generator import generate_script
from bridge.plugins.producer.seed_interpreter import interpret_seed
from bridge.services.llm_backends import (
    ChatBackend,
    VisionBackend,
    build_chat_backend,
    build_vision_backend,
)

logger = logging.getLogger(__name__)


@register_plugin
class ProducerPlugin(DJPlugin):
    name = "producer"
    description = "Seed-driven DJ producer with YAML-defined characters"
    version = "2.0.0"

    def __init__(self, ctx, instance_id=None, display_name=None):
        super().__init__(ctx, instance_id, display_name)
        self._characters: dict[str, CharacterConfig] = {}
        self._seed: SeedState | None = None
        self._script: Script | None = None
        self._executor: ScriptExecutor | None = None
        self._building = False
        self._build_fail_count = 0
        self._max_build_failures = 3
        self._signal_queue: asyncio.Queue = asyncio.Queue()

        # Config
        cfg = ctx.config
        self._plan_size = cfg.get("plan_size", 10)
        self._materialize_count = cfg.get("materialize_ahead", 4)
        self._upload_dir = _resolve_upload_dir(cfg)

        # LLM backends — one per role
        self._models_cfg: dict[str, dict] = dict(cfg.get("models") or {})
        self._interpreter_backend: ChatBackend = build_chat_backend(
            self._models_cfg.get("interpreter", {}), ctx.llm_service,
        )
        self._script_backend: ChatBackend = build_chat_backend(
            self._models_cfg.get("script_generator", {}), ctx.llm_service,
        )
        self._vision_backend: VisionBackend = build_vision_backend(
            self._models_cfg.get("vision", {}), ctx.llm_service.endpoint,
        )

    # =====================================================================
    # LIFECYCLE
    # =====================================================================

    async def on_start(self) -> None:
        cfg = self.ctx.config

        # Load characters
        self._characters = load_characters(cfg)
        if not self._characters:
            self.logger.warning("No characters defined — producer will not start")
            return

        self._executor = ScriptExecutor(self.ctx, self._characters)

        # Flush + register as feeder (same pattern as before)
        await self.ctx.mixer.flush_music_queue()
        if self.ctx.playlist_planner:
            planner = self.ctx.playlist_planner
            planner._upcoming.clear()
            if planner._db:
                await planner._db.execute("DELETE FROM playlist_queue")
                await planner._db.commit()
            self.logger.info("Flushed planner queue (memory + DB + Liquidsoap)")
            planner.set_feeder(self)

        # Listen for stream events
        self.listen("track_changed", self._on_track_changed)
        self.listen("skip", self._on_skip)

        # Start signal processing loop
        self.create_task(self._signal_loop())

        # Initial seed: default_character from config, or first character
        default_id = cfg.get("default_character", "")
        if default_id and default_id in self._characters:
            initial_cast = [default_id]
        else:
            initial_cast = [next(iter(self._characters))]
        self.logger.info(
            f"Producer starting with default host '{initial_cast[0]}' "
            f"({len(self._characters)} characters loaded). Models: "
            f"interpreter={self._interpreter_backend.name}/{self._interpreter_backend.model}, "
            f"script={self._script_backend.name}/{self._script_backend.model}, "
            f"vision={self._vision_backend.name}/{self._vision_backend.model}"
        )
        await self._signal_queue.put(("seed", {"cast": initial_cast, "_silent_default": True}))

    async def on_stop(self) -> None:
        if self.ctx.playlist_planner:
            self.ctx.playlist_planner.clear_feeder()

    # =====================================================================
    # SELECTION STRATEGY (called by PlaylistPlanner._fill_queue)
    # =====================================================================

    async def select_next(
        self,
        library: list[dict],
        history: list[dict],
        upcoming: list[dict],
    ) -> dict | None:
        """Serve the next track from the script."""
        if not self._script:
            return None

        upcoming_paths = {t["file_path"] for t in upcoming}

        for segment in self._script.remaining:
            if segment.state == "done" or segment.served:
                continue
            fp = segment.track["file_path"]
            if fp in upcoming_paths:
                continue
            if not Path(fp).exists():
                self.logger.warning(f"Scripted track missing: {fp}")
                segment.state = "done"
                continue
            segment.served = True
            return segment.track

        # Script exhausted — request rebuild
        if not self._building:
            await self._signal_queue.put(("buffer_low", {}))

        # Degraded fallback if LLM keeps failing
        if self._build_fail_count >= self._max_build_failures and self._primary_char():
            history_paths = {t["file_path"] for t in history[:20]}
            songs = select_songs(library, self._primary_char(), 1, history_paths, upcoming_paths)
            if songs:
                self.logger.warning("Script building failing — degraded to random selection")
                return songs[0]

        return None

    # =====================================================================
    # SIGNAL LOOP
    # =====================================================================

    async def _signal_loop(self) -> None:
        """Central event loop processing signals one at a time."""
        while True:
            try:
                signal_type, payload = await asyncio.wait_for(
                    self._signal_queue.get(), timeout=60.0
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return

            try:
                handler = {
                    "seed": self._handle_seed,
                    "buffer_low": self._handle_buffer_low,
                    "skip": self._handle_skip,
                    "quickrun": self._handle_quickrun,
                    "mood": self._handle_mood,
                }.get(signal_type)

                if handler:
                    await handler(payload)
                else:
                    self.logger.warning(f"Unknown signal: {signal_type}")
            except Exception:
                self.logger.exception(f"Error handling signal: {signal_type}")

    # =====================================================================
    # SIGNAL HANDLERS
    # =====================================================================

    async def _handle_seed(self, payload: dict) -> None:
        """New seed arrived: interpret → ack → soft-flush → rebuild.

        Smooth handover: the outgoing host's voice briefly acknowledges the
        change, current + next track keep playing so music never drops to
        silence, and the new script fills in as the bridge drains.
        """
        library = self._library()
        silent_default = bool(payload.pop("_silent_default", False))

        old_seed = self._seed
        old_host = self._primary_char()

        try:
            seed = await interpret_seed(
                payload,
                characters=self._characters,
                library=library,
                interpreter=self._interpreter_backend,
                vision=self._vision_backend,
                upload_dir=self._upload_dir,
            )
        except Exception:
            self.logger.exception("Seed interpretation failed; keeping previous seed")
            return

        # Fire an ack voice line from the outgoing host (in parallel with build).
        # Only when there's a real handover — not on the initial default seed,
        # and only if an old host was on-air.
        if not silent_default and old_seed is not None and old_host is not None:
            self.create_task(self._speak_seed_ack(old_host, seed))

        self._seed = seed
        if not silent_default:
            self.logger.info(f"New seed: {seed.pipeline} → {seed.interpretation_notes}")
            # Soft flush: keep a couple of bridge tracks so music continues.
            await self._soft_flush()
        else:
            self.logger.info(f"Initial seed (default): {seed.interpretation_notes}")

        await self._build_script()

    async def _handle_buffer_low(self, payload: dict) -> None:
        await self._build_script()

    async def _handle_skip(self, payload: dict) -> None:
        if not self._primary_char():
            return
        primary = self._primary_char()
        track_info = payload.get("track_info", {})

        planner = self.ctx.playlist_planner
        if planner:
            starred = await planner.get_starred_tracks(min_stars=1)
            if starred:
                pick = random.choice(starred)
                await planner.insert_track(pick["file_path"], position=0)

        skipped = f"{track_info.get('artist', '?')} — {track_info.get('title', '?')}"
        try:
            reaction = await self._script_backend.chat(
                f'The listener just skipped "{skipped}". React briefly in-character (1 sentence).',
                system_prompt=primary.personality,
            )
            await self.say(
                reaction,
                trigger="asap",
                priority=30,
                speaker=primary.voice_speaker,
                instruct=primary.voice_instruct,
            )
        except Exception:
            self.logger.exception("Skip reaction failed")

        if self._script and len(self._script.remaining) < 3:
            await self._build_script()

    async def _handle_quickrun(self, payload: dict) -> None:
        """Inject a one-off segment without replanning."""
        primary = self._primary_char()
        if not primary:
            return

        topic = payload.get("topic", "weather")
        provider_map = {
            "weather": ["weather", "datetime"],
            "mail": ["work_tasks"],
            "traffic": ["datetime"],
        }
        providers = provider_map.get(topic, ["datetime"])
        context = await gather_context(
            self.ctx, primary,
            producer_config=self.ctx.config,
            providers=providers,
        )

        import json as _json
        prompt = (
            f"Give a brief {topic} update for the radio audience (1-2 sentences, in character). "
            f"Context: {_json.dumps(context)}"
        )
        try:
            text = await self._script_backend.chat(prompt, system_prompt=primary.personality)
            await self.say(
                text,
                trigger="between_songs",
                priority=20,
                speaker=primary.voice_speaker,
                instruct=primary.voice_instruct,
            )
            self.logger.info(f"Quickrun '{topic}' injected")
        except Exception:
            self.logger.exception(f"Quickrun '{topic}' failed")

    async def _handle_mood(self, payload: dict) -> None:
        """Legacy mood tweak — updates active character's weights in-memory."""
        new_weights = payload.get("genre_weights", {})
        primary = self._primary_char()
        if primary and new_weights:
            primary.genre_weights.update(
                {k.lower(): float(v) for k, v in new_weights.items()}
            )
            self.logger.info(f"Genre weights updated: {primary.genre_weights}")
            await self._build_script()

    # =====================================================================
    # SCRIPT BUILDING
    # =====================================================================

    async def _build_script(self) -> None:
        """Build in two phases: songs first (fast, push to LS immediately),
        then LLM voice cues merged onto future segments while music plays.

        Phase 1 (songs):  gather_context + select_songs + queue to Liquidsoap
                          → music flows in < ~2 s
        Phase 2 (voice):  LLM script → merge voice_cues onto segments not yet
                          playing → TTS materialize_ahead
                          → voice catches up over ~30-90 s
        """
        if self._building:
            return
        primary = self._primary_char()
        if not primary:
            self.logger.warning("No primary character for build — skipping")
            return
        self._building = True

        try:
            import time as _time

            # ============================================================
            # Phase 1: songs (fast path — music starts flowing ASAP)
            # ============================================================
            context = await gather_context(
                self.ctx, primary,
                producer_config=self.ctx.config,
            )

            planner = self.ctx.playlist_planner
            history_paths: set[str] = set()
            upcoming_paths: set[str] = set()
            library: list[dict] = []

            if planner:
                library = planner.library
                history = await planner.get_history(limit=50)
                history_paths = {h["file_path"] for h in history}
                upcoming_paths = {t["file_path"] for t in planner.upcoming}

            songs = self._select_songs_with_seed(
                library, primary, self._plan_size, history_paths, upcoming_paths,
            )
            if not songs:
                self.logger.warning("No songs available for script")
                self._build_fail_count += 1
                return

            active_chars = self._active_cast()

            # Preliminary script: songs only, no voice cues yet. `state="ready"`
            # so _on_track_changed won't try to materialize anything (voice_cues
            # is empty so nothing fires anyway).
            self._script = Script(
                segments=[
                    ScriptSegment(position=i, track=s, voice_cues=[], state="ready")
                    for i, s in enumerate(songs)
                ],
                primary_character=primary.id,
                cast=[c.id for c in active_chars],
                generated_at=_time.time(),
            )

            # Push the first batch to Liquidsoap NOW — before the LLM runs.
            if self.ctx.playlist_planner:
                await self.ctx.playlist_planner._deferred_fill()

            if self._seed is not None:
                self._seed.songs_queued_at = _time.time()
                queue_seconds = self._seed.songs_queued_at - self._seed.set_at
                self.logger.info(
                    f"[seed] {self._seed.pipeline} → {primary.name} :: "
                    f"phase 1 (songs) ready in {queue_seconds:.2f}s "
                    f"— {len(songs)} tracks queued"
                )

            # Hard flag: now that the new first song is in LS's queue, skip
            # the currently-playing track. Crossfade goes current → new first.
            if self._seed is not None and self._seed.hard:
                try:
                    await self.ctx.mixer.next_track()
                    self.logger.info("[seed] hard=true → skipped current track")
                except Exception:
                    self.logger.exception("hard=true skip failed")

            # ============================================================
            # Phase 2: voice cues (LLM + TTS — music already playing)
            # ============================================================
            try:
                llm_script = await generate_script(
                    chat_backend=self._script_backend,
                    character=primary,
                    songs=songs,
                    context=context,
                    active_characters=active_chars,
                    seed=self._seed,
                )
                self._merge_voice_cues(llm_script)
            except Exception:
                self.logger.exception("Phase 2 script generation failed; keeping silent script")

            # TTS for segments ahead of the cursor
            await self._executor.materialize_ahead(self._script, self._materialize_count)

            self._build_fail_count = 0

            # Stamp full build time + log the handover forecast
            if self._seed is not None:
                self._seed.built_at = _time.time()
                build_seconds = self._seed.built_at - self._seed.set_at
                remaining = self.ctx.stream_context.remaining_seconds if self.ctx.stream_context else 0.0
                expected_live = max(build_seconds, build_seconds + remaining) if remaining > 0 else build_seconds
                self.logger.info(
                    f"[seed] {self._seed.pipeline} → {primary.name} :: "
                    f"phase 2 (voice) built in {build_seconds:.1f}s total, "
                    f"expected on-air in ~{expected_live:.1f}s "
                    f"(current track {remaining:.1f}s remaining)"
                )

            self.logger.info(
                f"Script built: {len(self._script.segments)} segments, "
                f"cast=[{', '.join(self._script.cast)}], "
                f"primary={primary.name}"
            )
        except Exception:
            self.logger.exception("Script building failed")
            self._build_fail_count += 1
        finally:
            self._building = False

    def _merge_voice_cues(self, llm_script: Script) -> None:
        """Overlay voice_cues from the LLM-built script onto the already-queued
        songs. Only applies to segments at or ahead of the cursor — the
        currently-playing segment keeps whatever cues it had (usually none)."""
        if not self._script:
            self._script = llm_script
            return

        # Map file_path → voice_cues from the LLM result
        cues_map: dict[str, list] = {}
        for seg in llm_script.segments:
            fp = seg.track.get("file_path")
            if fp:
                cues_map[fp] = seg.voice_cues

        cursor = self._script.cursor
        merged = 0
        # Start from cursor + 1 — don't modify a segment that's already on-air
        for seg in self._script.segments[cursor + 1:]:
            fp = seg.track.get("file_path")
            if fp in cues_map and cues_map[fp]:
                seg.voice_cues = cues_map[fp]
                seg.state = "planned"  # so materialize_ahead picks it up
                merged += 1

        self.logger.info(
            f"Voice cues merged onto {merged} upcoming segment(s) "
            f"(cursor={cursor}, total={len(self._script.segments)})"
        )

    def _select_songs_with_seed(
        self,
        library: list[dict],
        primary: CharacterConfig,
        count: int,
        history_paths: set[str],
        upcoming_paths: set[str],
    ) -> list[dict]:
        """Select `count` tracks with seed influence.

        If a seed song is present, place it first and fill the rest.
        `genre_focus` boosts matching genres (soft). When `seed.strict` is set,
        the library is hard-filtered to tracks whose genre contains any focus
        substring; untagged tracks are excluded. Falls back to soft selection
        if the filter would starve the script.
        """
        focus = list(self._seed.genre_focus) if self._seed else []
        effective = primary
        effective_library = library

        if focus:
            # Clone primary with genre_focus merged into weights (non-destructive)
            boosted = dict(primary.genre_weights)
            for g in focus:
                boosted[g.lower()] = max(boosted.get(g.lower(), 0.0), 10.0)
            effective = CharacterConfig(
                id=primary.id,
                name=primary.name,
                personality=primary.personality,
                voice_speaker=primary.voice_speaker,
                voice_instruct=primary.voice_instruct,
                genre_weights=boosted,
                avoid_genres=primary.avoid_genres,
            )

        # Strict mode: hard-filter library to focus genres (untagged excluded)
        if self._seed and self._seed.strict and focus:
            filtered = [
                t for t in library
                if any(f in (t.get("genre") or "").lower() for f in focus)
            ]
            # Need enough matching tracks to exclude history — allow 2× headroom
            needed = count + len(history_paths.intersection({t["file_path"] for t in filtered}))
            if len(filtered) >= max(count, needed):
                effective_library = filtered
                self.logger.info(
                    f"Strict genre filter: {len(filtered)} of {len(library)} "
                    f"tracks match {focus}"
                )
            else:
                self.logger.warning(
                    f"Strict filter produced only {len(filtered)} tracks for "
                    f"{focus} — falling back to soft selection to avoid starvation"
                )

        songs: list[dict] = []
        if self._seed and self._seed.first_song:
            songs.append(self._seed.first_song)
            self._seed.first_song = None  # consume — one-shot

        remaining = count - len(songs)
        if remaining > 0:
            seed_paths = {s["file_path"] for s in songs}
            picked = select_songs(
                effective_library, effective, remaining,
                history_paths | seed_paths, upcoming_paths | seed_paths,
            )
            songs.extend(picked)
        return songs

    # =====================================================================
    # EVENT HANDLERS
    # =====================================================================

    async def _on_track_changed(self, track_info: dict) -> None:
        if not self._script or not self._executor:
            return

        filename = track_info.get("filename", "")
        segment = self._find_segment(filename)
        if not segment:
            return

        self._script.cursor = segment.position
        segment.state = "playing"

        # First on-air track of this seed → stamp live_at + log handover total
        if self._seed is not None and self._seed.live_at is None:
            import time as _time
            self._seed.live_at = _time.time()
            handover = self._seed.live_at - self._seed.set_at
            primary = self._primary_char()
            self.logger.info(
                f"[seed] {self._seed.pipeline} → "
                f"{primary.name if primary else '?'} :: LIVE on-air "
                f"(handover {handover:.1f}s from seed) — "
                f"'{segment.track.get('artist','?')} — {segment.track.get('title','?')}'"
            )

        if segment.voice_cues:
            if segment.state != "ready":
                await self._executor.materialize_segment(segment)
            await self._executor.schedule_voice(segment)

        self.create_task(
            self._executor.materialize_ahead(self._script, self._materialize_count)
        )

    async def _on_skip(self, track_info: dict) -> None:
        await self._signal_queue.put(("skip", {"track_info": track_info}))

    # =====================================================================
    # HELPERS
    # =====================================================================

    async def _flush(self) -> None:
        """Hard flush — drop everything and silence the feed. Used at startup."""
        self._script = None
        await self.ctx.mixer.flush_music_queue()
        planner = self.ctx.playlist_planner
        if planner:
            planner._upcoming.clear()
            if planner._db:
                await planner._db.execute("DELETE FROM playlist_queue")
                await planner._db.commit()

    async def _soft_flush(self) -> None:
        """Clear ALL pending queue (Python + Liquidsoap) without touching the
        currently-playing track. The current song finishes naturally, then
        Liquidsoap crossfades into whatever the new script queues first.

        Timing assumption: a new script finishes building (~30–120 s) well
        before the current track ends (~4 min average), so Liquidsoap always
        has the new first song ready when it needs it.
        """
        self._script = None  # stops future voice cues from the old script
        planner = self.ctx.playlist_planner
        if planner:
            planner._upcoming.clear()
            if planner._db:
                await planner._db.execute("DELETE FROM playlist_queue")
                await planner._db.commit()

        await self.ctx.mixer.flush_queued_music()
        self.logger.info("Soft flush: cleared pending queue, current track keeps playing")

    async def _speak_seed_ack(self, old_host: CharacterConfig, new_seed) -> None:
        """Quick voice line from the outgoing host acknowledging the new seed.

        Runs in parallel with the script build. Scheduled as 'between_songs'
        so it slots naturally into the next gap.
        """
        try:
            new_cast_names = ", ".join(
                self._characters[c].name
                for c in new_seed.cast
                if c in self._characters
            ) or "a new direction"

            hint_parts: list[str] = []
            if new_seed.first_song:
                hint_parts.append(
                    f"first track: {new_seed.first_song.get('artist','?')} — "
                    f"{new_seed.first_song.get('title','?')}"
                )
            if new_seed.genre_focus:
                hint_parts.append(f"genre: {', '.join(new_seed.genre_focus[:3])}")
            if new_seed.mood_text:
                hint_parts.append(f"mood: {new_seed.mood_text}")
            hint = "; ".join(hint_parts) or new_seed.interpretation_notes

            # Skip ack if the handover is nominal (same single host, no change)
            if (
                new_seed.cast
                and len(new_seed.cast) == 1
                and new_seed.cast[0] == old_host.id
                and not new_seed.first_song
                and not new_seed.genre_focus
            ):
                return

            prompt = (
                f"A listener request just came in. Details: {hint}. "
                f"In ONE brief sentence, acknowledge the request and hand off to "
                f"{new_cast_names}. Stay in character. No hashtags, emojis, or markdown."
            )
            line = await self._script_backend.chat(
                prompt, system_prompt=old_host.personality
            )
            line = (line or "").strip()
            if not line:
                return
            await self.say(
                line,
                trigger="between_songs",
                priority=10,  # higher than regular cues (lower number = higher priority)
                speaker=old_host.voice_speaker,
                instruct=old_host.voice_instruct,
                leading_silence=0.4,
                trailing_silence=0.3,
            )
            self.logger.info(
                f"Seed ack queued (outgoing={old_host.id}): {line[:80]}"
            )
        except Exception:
            self.logger.exception("Seed ack generation failed")

    def _library(self) -> list[dict]:
        planner = self.ctx.playlist_planner
        return planner.library if planner else []

    def _primary_char(self) -> CharacterConfig | None:
        if self._seed and self._seed.cast:
            return self._characters.get(self._seed.cast[0])
        if self._characters:
            return next(iter(self._characters.values()))
        return None

    def _active_cast(self) -> list[CharacterConfig]:
        if self._seed and self._seed.cast:
            return [self._characters[c] for c in self._seed.cast if c in self._characters] \
                   or [next(iter(self._characters.values()))]
        return [next(iter(self._characters.values()))]

    def _find_segment(self, filename: str) -> ScriptSegment | None:
        if not self._script:
            return None
        fname = Path(filename).name if filename else ""
        for seg in self._script.segments:
            if Path(seg.track.get("file_path", "")).name == fname:
                return seg
        return None

    # =====================================================================
    # PUBLIC API (called by web routes)
    # =====================================================================

    async def submit_seed(self, raw: dict) -> None:
        """Queue a new seed for processing. Flushes + rebuilds."""
        await self._signal_queue.put(("seed", dict(raw)))

    async def request_skip(self, track_info: dict | None = None) -> None:
        await self._signal_queue.put(("skip", {"track_info": track_info or {}}))

    async def request_quickrun(self, topic: str = "weather") -> None:
        await self._signal_queue.put(("quickrun", {"topic": topic}))

    async def adjust_mood(self, genre_weights: dict) -> None:
        await self._signal_queue.put(("mood", {"genre_weights": genre_weights}))

    def update_models(self, models_cfg: dict) -> dict:
        """Runtime swap of any of the three backends. Returns the new active config.

        Body shape: {"interpreter": {"backend": "claude_cli", "model": "haiku"}, ...}
        Only mentioned roles are updated.
        """
        for role, role_cfg in (models_cfg or {}).items():
            if not isinstance(role_cfg, dict):
                continue
            merged = {**self._models_cfg.get(role, {}), **role_cfg}
            if role == "interpreter":
                self._interpreter_backend = build_chat_backend(merged, self.ctx.llm_service)
            elif role == "script_generator":
                self._script_backend = build_chat_backend(merged, self.ctx.llm_service)
            elif role == "vision":
                self._vision_backend = build_vision_backend(merged, self.ctx.llm_service.endpoint)
            else:
                self.logger.warning(f"Ignoring unknown model role: {role}")
                continue
            self._models_cfg[role] = merged
            self.logger.info(f"Swapped {role} backend → {merged}")
        return self.models_status

    @property
    def models_status(self) -> dict:
        return {
            "interpreter": {"backend": self._interpreter_backend.name, "model": self._interpreter_backend.model},
            "script_generator": {"backend": self._script_backend.name, "model": self._script_backend.model},
            "vision": {"backend": self._vision_backend.name, "model": self._vision_backend.model},
        }

    @property
    def status(self) -> dict:
        primary = self._primary_char()
        return {
            "active_character": primary.id if primary else None,
            "character_name": primary.name if primary else None,
            "cast": list(self._seed.cast) if self._seed else [],
            "characters": list(self._characters.keys()),
            "seed": self._seed.as_dict() if self._seed else None,
            "models": self.models_status,
            "script_segments": len(self._script.segments) if self._script else 0,
            "script_cursor": self._script.cursor if self._script else 0,
            "script_remaining": len(self._script.remaining) if self._script else 0,
            "building": self._building,
        }

    @property
    def plan_detail(self) -> list[dict]:
        if not self._script:
            return []
        result = []
        for seg in self._script.segments:
            result.append({
                "position": seg.position,
                "track": {
                    "artist": seg.track.get("artist", "?"),
                    "title": seg.track.get("title", "?"),
                    "genre": seg.track.get("genre", "?"),
                    "file_path": seg.track.get("file_path", ""),
                },
                "voice_cues": [
                    {
                        "character": vc.character_id,
                        "style": vc.style,
                        "text": vc.text,
                        "has_audio": vc.tts_audio is not None,
                        "duration": vc.tts_duration,
                    }
                    for vc in seg.voice_cues
                ],
                "state": seg.state,
            })
        return result

    @classmethod
    def config_fields(cls) -> list[dict]:
        return [
            {"key": "default_character", "type": "text", "label": "Default character ID"},
            {"key": "plan_size", "type": "number", "label": "Songs per script", "default": 10},
            {"key": "materialize_ahead", "type": "number", "label": "Pre-generate TTS count", "default": 4},
        ]


def _resolve_upload_dir(cfg: dict) -> Path:
    """Where to save uploaded seed images. Defaults to <station>/uploads/seeds/."""
    import os
    station_env = os.environ.get("RADIODAN_STATION_DIR")
    if station_env:
        return Path(station_env) / "uploads" / "seeds"
    # Fallback to tmp if no station dir (shouldn't happen in prod)
    return Path("/tmp/radiodan_seed_uploads")
