# RadioDan v2 — Health Checkup, 2026-05-08 09:00 CEST

## TL;DR

**The radio is broadcasting a frozen buffer.** systemd says "active (running)", Icecast keeps pushing ~128 kbps to listeners, but Liquidsoap has been stuck at the end of one track ("magi - the stem") for **6 days**. The Python bridge's watchdog has been firing every 10 seconds since 2026-05-02 08:51 UTC without recovering. No track changes, no DJ talk, no logging since then.

No changes made. Diagnosis only.

## Uptime

| Component | Up since (UTC) | Duration |
|---|---|---|
| `radiodan-v2.service` (systemd user) | 2026-04-27 08:56 | 11d |
| Liquidsoap (telnet `uptime`) | 2026-04-27 ~08:56 | 10d 22h |
| Icecast `server_start` | 2026-04-27 08:56 | 11d |

Service has not crashed, no restarts.

## Stream

- `http://localhost:49996/stream` — delivering audio, ~23 KB/s (≈realtime+ at 128 kbps).
- Icecast title metadata frozen at "magi - the stem".
- Listeners now: 0. Peak this session: 2.

## Stats (from `stations/radio-dan/radiodan.db`)

- **Library:** 8377 tracks indexed.
- **Plays logged:** 5703 total in `playlist_history`.
- **First play:** 2026-04-15 15:56 UTC.
- **Last play:** 2026-05-02 06:46:53 UTC ← *freeze point*.
- **Daily plays during healthy period:** 300–370/day, ~340 average.
- **2026-05-02 plays:** 105 (truncated at the freeze).
- **2026-05-03 → today:** 0 plays.

Top played tracks (the in-character "favorites"):

| Plays | Artist | Title |
|---|---|---|
| 161 | The Corrs | Only When I Sleep |
| 161 | Led Zeppelin | Whole Lotta Love |
| 161 | gangster blacc | pretty, ain't nothing nice |
| 161 | West Side Connection | King Of The Hill |
| 160 | 2000_41 Da Buzz | Let me love you tonight |
| 160 | ice t | 6 n the morning |

Voice segments completed (talk lanes):
- producer: 1182
- bad-mouth-bob: 84
- time/dong: 397
- producer (failed): 401, (cancelled): 414

All voice activity also stopped on 2026-05-02 06:39 UTC.

## The freeze — what we know

**Timeline (2026-05-02 UTC):**

```
08:46:53  TRACK changed → "magi — the stem" (duration 273.713s)
08:50:52  "Track ending in 28.9s" (last normal pre-end warning)
08:51:30  WATCHDOG: "Liquidsoap queue empty but planner has 5 tracks — re-pushing"
08:51:40  WATCHDOG: same, re-pushing
... every 10 seconds, for 6 days, still firing as of 09:00 today.
```

**Liquidsoap state right now (telnet):**

```
music.elapsed       = 273.713310658
music.remaining     = 0.0
music_q.queue_length = 0
request.all         = (empty)
request.metadata 261433 = "No such request."
```

The track's `duration_seconds` in the DB is `273.713310657596` — Liquidsoap is paused **exactly at the end of "magi - the stem"**. It finished the song and never advanced.

**The smoking gun:** `bridge.audio.mixer` keeps pushing tracks via `music_q.push <uri>`. Liquidsoap accepts each push and assigns a request id (rids are now in the **261,000+** range — over a quarter-million pushes since the freeze). But none of them enter the playable queue: `music_q.queue_length` is 0, and `request.metadata` for the most recently-issued rid says "No such request." Pushes are succeeding at the protocol level but the requests are being silently dropped (or instantly evicted) inside Liquidsoap.

The watchdog at `bridge.audio.stream_context` correctly detects the empty queue ("Liquidsoap queue empty but planner has 5 tracks — re-pushing") but its only response is to re-push the same 5 tracks. There is no escalation: the watchdog never restarts Liquidsoap or the service even after thousands of failed recoveries.

## Producer plugin state

```
active             = true
character          = "bob" (Bad Mouth Bob)
cast               = ["bob"]
script_cursor      = 0
script_remaining   = 10
building           = false
seed.timing.set_at = 2026-05-02 (≈4.9 days before songs_queued_at)
```

The producer believes it has a 10-segment script ready but the cursor never advances because Liquidsoap stops emitting `track_play` end-events.

## Root cause (confirmed)

**Broken symlinks under `music/_damaged/` shipped as playable tracks.**

`scan_mp3_health.sh` (untracked, in repo root) is an MP3 health scanner: it runs ffmpeg on every track, flags files with decode errors or long silence, and writes a symlink for each into `music/_damaged/` "for Samba browsing." It uses `realpath` on the music dir, so the symlinks are written with **absolute host paths**:

```
$ readlink "/home/dln/dev/radiodan-agent/music/_damaged/_BigOldArchive - mp3 - cypress hill - whats your number.mp3"
/home/dln/dev/radiodan-agent/music/_BigOldArchive/mp3/cypress hill - whats your number.mp3
```

Liquidsoap runs in a Docker container (`savonet/liquidsoap:v2.4.2`) with `./music:/music:ro`. From inside that container, the symlink target `/home/dln/...` doesn't exist — only `/music` does. Result, from inside the container:

```
$ test -f "/music/_damaged/_BigOldArchive - mp3 - cypress hill - whats your number.mp3"
MISSING
```

All 28 symlinks in `_damaged/` are broken from the container's perspective. Liquidsoap logs confirm:

```
[request:3] Nonexistent file or ill-formed URI "/music/_damaged/..."
```

But — critically — `_damaged/` is *under* `/music`, so the playlist planner indexed those symlinks into `music_library` as normal tracks. The producer eventually picked five `_damaged/` files for a script segment. Liquidsoap finished "magi - the stem", tried to resolve the next request, hit "Nonexistent file" on every retry, and got stuck. The crossfade chain plus a 1-second `sine` "silence" fallback meant nothing else could take over.

## Why nothing caught it — six layered defenses, all absent or broken

| Layer | What could have stopped this | What actually happened |
|---|---|---|
| Scanner script | Use relative symlinks (`ln -sr`), or put `_damaged/` outside `/music`, or just write the report (no symlinks) | Wrote absolute-host-path symlinks under the audio mount |
| Library indexer | Exclude `_damaged/` (or any `_*` dir) from scan | Indexed all 28 broken symlinks as playable tracks |
| Producer plugin | Don't pick known-damaged files for the script | Picked five in a row |
| Bridge mixer | Verify the path resolves to a real file via the container's view before pushing | Pushed blindly; treated the rid response as success |
| Liquidsoap silence fallback | Infinite always-ready source | `sine(duration=1.0)` ends after one second |
| Bridge watchdog | Detect *lack of progress* (elapsed not advancing) and escalate | Re-pushed the same broken tracks every 10s for 6 days |

Each one alone would have stopped the freeze. None of them did.

## Prevention plan

Three layers, ordered by leverage. The middle layer is Dan's idea and is the most elegant of the bunch — it would have caught this in ~5 minutes regardless of which underlying defense failed.

### Layer 1 — Detect & recover (works for any Liquidsoap stall, not just this one)

**Track-bounded watchdog (Dan's idea).** Don't poll on a fixed interval. When a track starts, set a deadline: `expected_end = elapsed_now + duration_remaining + 10s grace`. If that deadline passes without a `track_changed` event, the stream is stuck — escalate immediately. Eliminates the "every 10s, forever" failure mode and is independent of *why* Liquidsoap stalled.

**Pre-queue duration sanity check (Dan's idea).** Before the planner pushes a batch, look at `duration_seconds` on the upcoming tracks. If all of them are `< 10s`, that's a heuristic for "something is off" (broken files often have zero/tiny durations after metadata extraction failures). Refuse to push that batch and pick again.

**Escalation ladder when watchdog trips:**

1. `music_q.skip` — force-advance past the stuck request.
2. Flush queue + push a known-good fallback track (see Layer 2).
3. Flush queue + push a fresh batch from the planner.
4. `docker restart radiodan-agent-liquidsoap-1` — last resort.

### Layer 2 — A "known-good" fallback (Dan's idea)

Keep one or two designated bulletproof tracks (a stable, verified MP3 the system trusts) ready to queue at any moment. Used by the watchdog escalation ladder above and as the producer's last-resort if its plan-building fails. Better than dead-air silence: listeners hear something the station owner picked, not 30 minutes of nothing.

This is the dial that turns "the radio froze" into "the radio briefly played the station's anthem and recovered."

### Layer 3 — Validate before queueing (catches this exact class of bug)

In `LiquidsoapMixer.queue_music`, before sending `music_q.push`:

- `Path(host_equivalent).resolve(strict=True)` — fails fast on broken symlinks.
- Check the resolved target is still under the music mount root — refuses paths that escape the container's view.

A logged "skipped: unreachable target" beats a silent rid drop. Lets the planner mark the file unplayable and pick another.

### Layer 4 — Liquidsoap-side safety net

In `config/station.liq:46`:

```diff
- silence = sine(duration=1.0, 440.0)
+ silence = blank()
```

`blank()` is an infinite always-ready source. The current sine ends after one second, which contributes to the unrecoverable state when `music_queue` dies during a crossfade. With `blank()`, Icecast keeps streaming dead air, but the system stays in a state the watchdog can recover from.

### Layer 5 — Stop the bug at the source

- Have `playlist_planner` skip directories starting with `_` (or specifically `_damaged/`). The `_` prefix already signals "internal/special."
- Mark `_damaged/` rows as unplayable in `music_library` (e.g., a `playable=0` flag). The scanner already knows which files are damaged; surface that fact in the DB the planner reads.
- Fix `scan_mp3_health.sh`: either drop the symlink behavior (the report is enough), or use `ln -sr` for relative symlinks so they work inside the container too. Or move `_damaged/` out of `/music` entirely.

## Suggested rollout order

1. **Ship Layer 1 first** (track-bounded watchdog + escalation ladder). Standalone benefit, no schema changes, fixes the recovery story for *any* future stall.
2. **Add Layer 2** (known-good fallback track config). Small DB/config change. Big UX win on failure.
3. **Add Layer 3** (pre-push file validation). Defensive, prevents the silent-rid-drop class.
4. **Layer 4** (`blank()`) is a one-line config change; bundle with anything.
5. **Layer 5** is the cleanup pass — remove the inputs that triggered this, so the upper layers never have to fire.

## Quick fix for *right now*

`systemctl --user restart radiodan-v2.service` will rebuild Liquidsoap and the producer script. The planner will pick a fresh batch — likely *not* all from `_damaged/`, because the producer's selection is partly random — so the radio will probably play music again. But until the prevention layers above are in, the same freeze can recur the next time the producer happens to pick a `_damaged/`-heavy segment.

## What is NOT broken

- systemd service supervision, log rotation, journal.
- API at :49997 — responsive, returns plausible state (it just doesn't *know* that nothing is playing).
- Icecast — accepting and serving the stream.
- Database — writable on demand (the `dong` plugin keeps successfully scheduling hourly chime events through today, even though they never play).
- Disk space, memory (peak 2.3 GB, currently 406 MB), CPU (2h 51m total over 11 days = idle).
