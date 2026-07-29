# RadioDan Producer API — Handoff Cheatsheet

Snapshot: 2026-04-17. Bridge running on `systemd --user` unit `radiodan-v2`.
Base URL: `http://localhost:49997`. Stream: `http://localhost:49996/stream`.

## `/api/content/` does NOT exist

Free-text seeding uses `POST /api/producer/seed` with a JSON body.
Example: `{"text": "spring"}` → interpreter LLM classifies (vibe pipeline)
→ picks best-fit host → rebuilds show. That's the one you're thinking of.

## Characters (current roster)

| id    | name          | voice       | top genre weights                                   |
|-------|---------------|-------------|-----------------------------------------------------|
| bob   | Bad Mouth Bob | Eric        | hip-hop 5, rock 3, soul 3, funk 2, blues 2          |
| snoop | Snoop Dogg    | Adrian      | g-funk 10, hip-hop 5, reggae 3, soul 2, funk 2      |
| lani  | Lani          | laniv3      | pop 5, funk 4, dance 4, indie 4, reggae 3           |
| viv   | Viv           | vivianv2    | jazz 5, soul 4, indie 4, bossa 3, folk 3            |

`laniv3` and `vivianv2` are routed to Chatterbox on `192.168.1.15:11700`;
`Eric` / `Adrian` / others hit local Qwen3 at `localhost:42001`. Routing is
keyed on speaker name via `audio.tts.voice_map` in `station.yaml`.

## Library snapshot

- 7 494 tracks, 552 hours, 44 GB
- 190 distinct genre strings
- **2 634 (35%) untagged** — strict genre seeds exclude untagged tracks
- Top 15 genres (by track count):

```
  575  other          160  reggae
  566  pop            139  oldies
  547  hip-hop        133  trance
  419  rock           113  blues
  367  rap             90  rock & roll
  200  rock/pop        80  r&b
  182  dance           66  top 40
                       65  electronic
```

SQLite indexes exist on `LOWER(genre)`, `LOWER(artist)`, and
`playlist_history.file_path` — genre/artist queries are fast.

## The one endpoint that matters: `POST /api/producer/seed`

Accepts any combination; first non-empty field wins per priority.

**Two-phase build. Never silent.**
- *Phase 1 (≈1-2 s)*: `gather_context` + `select_songs` + push new tracks to
  Liquidsoap's music_q. The current track keeps playing; new tracks are
  queued as next.
- *Phase 2 (≈30-90 s)*: LLM generates voice cues, they're merged onto
  segments beyond the cursor; TTS materializes. Music is already flowing.

```bash
# Switch host (single)
curl -X POST http://localhost:49997/api/producer/seed \
  -H 'Content-Type: application/json' -d '{"character":"snoop"}'

# Duo (ABAB dialogue, ≤10 exchanges per segment)
curl -X POST http://localhost:49997/api/producer/seed \
  -H 'Content-Type: application/json' -d '{"cast":["lani","viv"]}'

# Genre seed (strict by default — hard-filters library; synonym-expanded:
#   "hip-hop" also catches "rap"/"hip hop/rap", "chip" catches "chiptune"/"8bit", etc.)
curl -X POST http://localhost:49997/api/producer/seed \
  -H 'Content-Type: application/json' -d '{"genre":"rap"}'

# Relax strict mode on a genre seed (let unmatched tracks bleed in)
curl -X POST http://localhost:49997/api/producer/seed \
  -H 'Content-Type: application/json' -d '{"genre":"trance","strict":false}'

# Force strict on a non-genre seed (unusual; defaults are usually right)
curl -X POST http://localhost:49997/api/producer/seed \
  -H 'Content-Type: application/json' -d '{"text":"late night jazz","strict":true}'

# HARD — skip the currently-playing song as soon as the new first song is
# queued (still never silent: new first is in LS before skip fires).
curl -X POST http://localhost:49997/api/producer/seed \
  -H 'Content-Type: application/json' -d '{"genre":"dance","hard":true}'

# Free-text vibe (LLM interprets → pipeline + cast + genre_focus)
curl -X POST http://localhost:49997/api/producer/seed \
  -H 'Content-Type: application/json' -d '{"text":"spring"}'

# Song seed (first track = this song, host picked by LLM)
curl -X POST http://localhost:49997/api/producer/seed \
  -H 'Content-Type: application/json' -d '{"song":"/path/to/track.mp3"}'

# Image (multipart) — upload → vision model → text → interpreter
curl -X POST http://localhost:49997/api/producer/seed -F image=@photo.jpg

# Image by URL
curl -X POST http://localhost:49997/api/producer/seed \
  -H 'Content-Type: application/json' -d '{"image_url":"https://..."}'
```

### Request flags

| field    | applies to | default | what it does |
|----------|-----------|---------|--------------|
| `strict` | any       | `true` for genre-seed; `false` elsewhere | When set, hard-filter the library to tracks whose genre contains a focus term. Untagged tracks are excluded. Auto-falls-back to soft if fewer than `plan_size` tracks match. |
| `hard`   | any       | `false` | After the new first song is pushed to Liquidsoap, call `music_q.skip` so LS crossfades out of the current song immediately. Never silent. |

### Seed handover semantics

1. Seed received → logged `[seed] ... phase 1 (songs) ready in Ns`
2. Liquidsoap's *pending* queue is flushed (NOT the currently-playing track)
   via the custom `music_q.flush_queued` telnet command.
3. New tracks are pushed to Liquidsoap.
4. If `hard=true`, the current track is skipped; otherwise it plays to
   completion and crossfades into the new first song.
5. Outgoing host says a brief ack line in the gap between songs
   ("handing it off to X, here comes the new vibe" — LLM-generated).
6. LLM voice cues for future segments merge in as phase 2 finishes.
7. First new-script track hits air → logged
   `[seed] ... LIVE on-air (handover Ns from seed) — 'Artist — Title'`.

Timing fields are exposed on `/api/producer/status.seed.timing`:
`set_at`, `songs_queued_at`, `built_at`, `live_at`, `songs_queued_seconds`,
`build_seconds`, `total_handover_seconds`.

## Inspecting state

```bash
# Current seed + cast + models + script cursor + timing
curl -s http://localhost:49997/api/producer/status | python3 -m json.tool

# Current 10-segment script (tracks + voice cues + TTS status)
curl -s http://localhost:49997/api/producer/plan | python3 -m json.tool

# Live events feed (SSE)
curl -N http://localhost:49997/api/events

# Characters with active flag
curl -s http://localhost:49997/api/producer/characters

# Library
curl -s http://localhost:49997/api/library/stats          # totals + top 10 genres + untagged count
curl -s 'http://localhost:49997/api/library/genres?limit=50'
curl -s 'http://localhost:49997/api/library/genres?q=jazz'
curl -s 'http://localhost:49997/api/library?genre=rap&q=big'
```

## Runtime LLM backend swap (ephemeral; not written to YAML)

```bash
# Swap script generator to claude -p --model sonnet
curl -X PUT http://localhost:49997/api/producer/models \
  -H 'Content-Type: application/json' \
  -d '{"script_generator":{"backend":"claude_cli","model":"sonnet"}}'

# Back to Ollama
curl -X PUT http://localhost:49997/api/producer/models \
  -H 'Content-Type: application/json' \
  -d '{"script_generator":{"backend":"ollama","model":"gpt-oss:20b"}}'
```

Three independent model roles: `interpreter` (seed classification),
`script_generator` (full show script), `vision` (image → text, Ollama only).
Defaults in `stations/radio-dan/station.yaml` under `plugins.producer.models`.

## Other verbs (back-compat / convenience)

```bash
# Skip current track + character reacts in-voice (noisy on a handover —
# prefer `seed {"hard":true}` for genre/character changes)
curl -X POST http://localhost:49997/api/producer/skip

# Inject a single segment without rebuilding (topic: weather|mail|traffic)
curl -X POST http://localhost:49997/api/producer/quickrun \
  -H 'Content-Type: application/json' -d '{"topic":"weather"}'

# Character-switch shortcut (alias for /seed {"character":...})
curl -X POST http://localhost:49997/api/producer/switch \
  -H 'Content-Type: application/json' -d '{"character":"lani"}'
```

## Caveats

- Changing `plugins.producer.*` in `station.yaml` alone does NOT take effect
  — SQLite `plugin_instances.config` is authoritative after first migration.
  Sync by writing the YAML block into the SQLite row, then restart.
- 35% of library is genre-untagged. Strict seeds exclude them, which is
  usually what you want but can starve niche genres (chip tune: only 2 tracks).
  When that happens the plugin auto-falls-back to soft mode and logs a warning.
- Seed stickiness is by design: rebuilds use the same seed until you send a
  new one. The initial default at startup is `{character: bob}` (from
  `plugins.producer.default_character` in station.yaml).
- Seed state does NOT persist across bridge restart. Systemd restart = Bob
  back on the mic. Change `default_character` in the SQLite-synced config if
  you want a different restart default.
- `hard=true` triggers Liquidsoap's crossfade; the current track fades out
  over the crossfade duration (default 5 s) into the new first song. Not
  instantaneous — expect a 3-5 s blended transition.

## Source locations

- Producer plugin: `bridge/plugins/producer/plugin.py`
- Seed interpreter: `bridge/plugins/producer/seed_interpreter.py`
- Script generator (LLM prompt): `bridge/plugins/producer/script_generator.py`
- Script executor (TTS + voice scheduling): `bridge/plugins/producer/script_executor.py`
- LLM backends (ollama / claude -p): `bridge/services/llm_backends.py`
- Routes: `bridge/web/routes/producer.py`, `bridge/web/routes/library.py`
- Liquidsoap `music_q.flush_queued` command: `config/station.liq`
