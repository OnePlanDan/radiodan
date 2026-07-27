# RadioDan v2 — Health Checkup, 2026-07-27 20:30 CEST

## TL;DR

**The radio never stopped. The DJ did.**

For 41 days Radio Dan has broadcast uninterrupted music — 41 days of perfect
uptime, zero service restarts, no freezes, a watchdog that self-healed every
time it tripped. And in those same 41 days **not one word was spoken on air**.
The local Qwen TTS host died on 2026-06-16 at 16:23, ~72 minutes after a
reboot, and never came back. 11 674 speech attempts failed in a row last month
alone. Every health signal stayed green the whole time, because the *stream* was
genuinely fine.

The 2026-05-08 freeze work was a success — it fixed the failure mode it was
built for and this checkup found no trace of it. What it did not cover is the
inverse failure: music flowing, everything green, DJ gone.

Fixed in this pass: TTS failover across hosts, a boot-time endpoint probe, and a
voice watchdog that alerts when nothing reaches air. Voice is back on the fixed
build (verified end to end). Still outstanding: `sudo systemctl enable --now
qwen-tts`, which needs a password I don't have.

## Uptime

| Component | Up since | Duration |
|---|---|---|
| Host (`forge`) | 2026-06-16 15:11 CEST | 41d 5h |
| `radiodan-v2.service` | 2026-06-16 15:11:17 CEST | 41d, `NRestarts=0` |
| Icecast | 2026-06-16 13:11:22 UTC | 41d, source never dropped |
| Liquidsoap container | boot | 41d |
| **Qwen TTS (`:42001`)** | **died 2026-06-16 16:23** | **down 41d** |

Bridge memory 313 MB (peak 773 MB). CPU 11 h 33 m across 41 days — idle. Disk
34 % used, 1.2 TB free. Nothing is under pressure.

## Stats — last 30 days

| Metric | Value |
|---|---|
| Tracks played | 8 285 completed (~380/day incl. skips) |
| Distinct tracks aired | 2 330 of 7 702 in library |
| LLM script builds | 848, **all completed** |
| **Voice segments on air** | **0** |
| Voice segments failed | 3 082 (+618 stranded `scheduled`) |
| **TTS generations** | **0 succeeded, 11 674 failed** |
| API calls served | **4, all 404** (23 days retained) |
| Music style changes | **1**, on 2026-07-04 |
| Watchdog trips | ~10, every one recovered at strike 1 |

All-time for contrast: 26 415 track plays, 9 720 voice segments (3 655
completed), 25 317 TTS calls (8 302 completed / 17 015 failed). Library is
7 702 tracks, 574 hours, 193 distinct genre strings, 2 714 untagged.

## The outage

**Timeline**

```
2026-06-16 15:11  host reboots; radiodan-v2 comes up via systemd (enabled)
2026-06-16 15:11  Icecast + Liquidsoap containers come up; stream live
      ~15:11      Qwen TTS started by hand (qwen-tts.service is disabled)
2026-06-16 16:22:53  last successful TTS generation
2026-06-16 16:22:57  last voice segment on air — "N.W.A. called 2005? Lani's
                     file cabinet ..." (producer lane)
2026-06-16 ~16:23  Qwen TTS dies. Nothing restarts it.
2026-06-16 → 2026-07-27  music, and only music, for 41 days
```

**Root cause.** `/etc/systemd/system/qwen-tts.service` exists and is correct —
`Restart=always`, `RestartSec=5` — but it is **`disabled`**. `journalctl -u
qwen-tts` has no entries at all, so it has never run under systemd on this
boot. It was started by hand after the reboot; when that process died there was
no supervisor to bring it back, and the next reboot would not have started it
either.

**Why nothing caught it — four layers, all absent**

| Layer | What could have stopped this | What actually happened |
|---|---|---|
| Supervision | `qwen-tts.service` enabled, so `Restart=always` applies | Unit disabled; started by hand; died unsupervised |
| TTS failover | Fall back to the other TTS host on the LAN | Single endpoint per voice; one failure = segment lost |
| Boot health check | Probe TTS at startup and complain | Service started silently into a dead backend |
| Voice watchdog | Notice zero voice segments for N hours | Nothing watched voice at all |

The last one is the real gap. `/api/status/health` *did* expose a TTS health
field the whole time — and it was itself broken (it probed a path built by
trimming the endpoint, resolving to `/tts/speakers`, which always reported
unhealthy). But nothing polled it, so a wrong answer and a right answer would
have been equally invisible. **The data existed; no one was watching.**

Worse, a working TTS host sat idle on the LAN the entire time. Chatterbox at
`192.168.1.15:11700` answers `/api/health` with `{"status":"ok"}` and serves
`carlin, female_podcast, lani, laniv2, laniv3, nolwazi, snoop, testvoice,
vivian, vivianv2`. Bob's voice `Eric` had no `voice_map` entry, so it fell
through to the dead default and stopped there. Lani, Viv and Snoop would have
worked throughout — the station was one character switch away from having a DJ.

## API calls served: essentially zero

journald retains back to 2026-07-04 (2.4 GB, ~23 days). In that window the
aiohttp access log holds **4 requests, all 404**:

- 3 from a Mac browser on `10.10.0.7` (Jul 8) hitting `/`, `/favicon.ico`,
  `/stream` — 404 because the web UI was removed in the API refactor
- 1 `agentos-probe` on `/` (Jul 27)

No agent has driven the station through the API for at least 23 days. The last
call that can be dated is the July 4 seed.

## Music style: one change, 23 days ago

`POST /api/producer/seed` with `{"genre":"hip-hop","strict":true,"hard":true}`
at **2026-07-04 10:04:40**. Still the live seed, 23 d 10 h later.

Genre-per-day from `playlist_history × music_library` shows the switch cleanly.
Through Jul 3: broad mix — hip-hop, rock, pop, untagged, reggae, blues. From
Jul 4 to today: every single day ~95 % rap/hip-hop. `strict:true` hard-filters
the library, so the 2 714 untagged tracks and everything non-hip-hop have been
excluded for 23 days — rotation is 2 330 of 7 702 tracks. Not broken, but
probably not intended to last three weeks either.

## Bugs found

1. **`hard=true` is never cleared.** `bridge/plugins/producer/plugin.py:437`
   re-applies the seed's hard-skip on *every* rolling rebuild — ~28×/day. A
   listener gets a mid-song cut roughly every 50 minutes, from a flag meant to
   fire once at seed time. Nothing in the codebase resets `_seed.hard`.
2. **Seed timing clock anchored to the original seed.** `set_at` is never
   re-stamped, so the log reads `phase 1 (songs) ready in 2023882s` and
   `expected on-air in ~2024183s` — 23 days reported as build latency. Cosmetic,
   but it makes the log useless for spotting a genuinely slow build.
3. **`script_cursor` frozen at 0** with `script_remaining: 10`, indefinitely.
   The cursor can't advance because segments never materialize, so the rolling
   script never rolls — it just gets rebuilt.
4. **Instrumentation drift.** `playlist_history` records ~381 plays/day while
   `track_play` events record ~276/day: ~25 % of airplay never lands in
   `event_log`. Separately, `skip_count` is **2** all-time despite ~650 producer
   hard-skips, because that path doesn't set `_skip_pending`.
5. **`booth.log` has no rotation** (plain `FileHandler`, 46 MB and growing) and
   its lines carry **time only, no date** — which made this checkup materially
   harder than it needed to be.
6. **Test suite was dead.** `tests/conftest.py` still imported
   `bridge.web.routes.timeline`, renamed to `events.py` in the May refactor, so
   the whole suite failed at collection. Import fixed here; 6 tests remain stale
   (SSE tests call `/api/timeline/events`, now `/api/events`) and are untouched.

## What was fixed in this pass

Commit 1 — `3272adc`, recovery point. The May refactor had been running on air
for 41 days with **zero commits** since 2026-04-20: web UI → JSON API, new
`llm_backends.py`, split routes, the 5-layer freeze work. 44 files,
+1 085/−8 356, all uncommitted. Now committed unmodified, so the build that has
been broadcasting is recoverable. `radiodan.db.pre-stats` and `uploads/` were
left out and added to `.gitignore`.

Commit 2 — the three fixes.

**1. TTS failover** (`bridge/services/tts_service.py`). Each voice now declares
an ordered route chain: primary from `voice_map`, then `fallbacks` from config.
Every route is tried before a segment is given up. Because voice names are not
portable between backends, a fallback entry may substitute the speaker as well
as the host. Non-200 responses and empty bodies fall through too, not just
refused connections — a sick backend fails over exactly like a dead one. When a
fallback carries a segment it logs at ERROR and writes a booth line, so
degraded-but-working never silently becomes the new normal.

Configured for Radio Dan in `station.yaml`: `Eric` and `Aiden` → Chatterbox as
`carlin`; the three Chatterbox voices → local Qwen as `Aiden`. **`carlin` is a
guess** — a famously foul-mouthed comedian felt like the closest fit for Bad
Mouth Bob, but it's one line in `station.yaml` if you'd rather it were someone
else.

**2. Health check that works** (same file). `probe_endpoint()` replaces the
broken `/speakers` check. It GETs the endpoint's origin root and treats *any*
HTTP response as alive, which is deliberately backend-agnostic: Qwen serves
`GET /`, Chatterbox 404s it. The thing worth distinguishing is "process gone"
from "process up", and any response proves the latter. `health_report()` returns
per-endpoint reachability; `log_startup_health()` probes everything at boot and
logs unreachable hosts at ERROR. `/api/status/health` now reports per-endpoint
state instead of one opaque boolean.

**3. Voice watchdog** (`bridge/services/voice_watchdog.py`, new). Alerts when no
voice segment has reached air for `silence_alert_hours` (default 3 h, well above
the producer's ~50 min rebuild cycle). Deliberately different from the May
watchdog in two ways: it alerts *once* and then reminds at most every 6 h — the
May checkup found a watchdog logging every 10 s for six days, which is
indistinguishable from noise — and it names the unreachable endpoint in the
alert, so the message says what to go fix. Outages open a `voice_outage` row in
the event log and close it on recovery, making them queryable after the fact
instead of reconstructable only by log archaeology. Critically, when nothing has
*ever* succeeded it measures from process start — the restart-into-a-dead-host
case, which is precisely what happened on June 16.

Alerts surface as an `alerts[]` array on **`/api/status`**, not just on the
health endpoint. Anything already polling status sees an outage without knowing
to ask a second question.

**Verification**

- Live failover: Bob spoke through Chatterbox as `carlin` in 3.7 s with Qwen
  still dead — 1 397 862 bytes of real audio.
- Endpoint probes correctly report `:42001` DOWN, `192.168.1.15:11700` UP.
- 28 new tests in `tests/test_tts_failover.py` and
  `tests/test_voice_watchdog.py`: routing, speaker substitution, duplicate-route
  collapse, malformed config, failover on connection-refused / HTTP 500 / empty
  body, all-routes-dead, probe semantics, alert-once, reminder interval,
  recovery, the never-succeeded case, and that a crashing watchdog can't take
  the station down.
- Full suite: **68 passed, 6 failed** — the same 6 that fail at `3272adc`
  (verified by stashing). No regressions.

## Still to do

1. **`sudo systemctl enable --now qwen-tts`** — needs a password. Until then Bob
   talks as `carlin` via Chatterbox, which is the failover working as designed,
   not a fix. Enabling it also means the next reboot brings TTS back on its own,
   which is the actual lesson of June 16.
2. **Decide the music style.** Strict hip-hop for 23 days, ~70 % of the library
   excluded. Intentional or a forgotten seed?
3. **Fix `hard=true` persistence** — bug 1 above. One mid-song cut every ~50
   minutes is the most listener-visible defect left.
4. **`booth.log`**: rotation, and dates on the lines.
5. **Stale SSE tests** — 6 failures pointing at the old `/api/timeline/events`.
6. **Instrumentation drift** — bug 4; `play_count` feeds producer song
   selection, so it's worth knowing which counter to trust.

## What is NOT broken

- Stream delivery. 41 days, source never dropped, no dead air.
- The May 8 freeze prevention. Watchdog tripped ~10 times in 22 days and
  recovered at strike 1 every time — never needed the fallback track or the
  container restart.
- Liquidsoap, Icecast, the database, systemd supervision of the bridge.
- LLM path: 848 script builds last month, all completed, local ollama
  `gpt-oss:20b`. `gpt-oss:20b` and `gemma4:26b` both present.
- Resources: idle CPU, flat memory, 1.2 TB free.
- Chatterbox TTS — healthy the entire time nobody was using it.
