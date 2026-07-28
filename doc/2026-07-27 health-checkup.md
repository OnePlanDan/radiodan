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
7. **~6 % of producer builds are silent, and nothing retries.** Found while
   verifying the fix: `script_generator._parse_response` rejects the LLM's JSON
   and logs `LLM returned invalid JSON, falling back to silent script`, which
   yields `Voice cues merged onto 0 upcoming segment(s)`. Across the retained
   journal that is **36 of ~631 builds** — roughly 1 in 17 rebuild cycles has no
   DJ at all, even when TTS is perfectly healthy. There is no retry: one bad
   parse costs the whole ~50-minute cycle. This is a second, independent channel
   for losing voice, and it will keep costing segments now that TTS works. A
   single re-ask on parse failure would recover most of it.

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

**One self-inflicted bug, caught in deployment.** The first restart put
`voice_watchdog` into `ctx_kwargs`, which `main.py` splats into `PluginContext`
— a fixed-field dataclass. Both plugins died with `TypeError: unexpected keyword
argument 'voice_watchdog'` and the station carried on streaming music with no
DJ, which is the very failure being fixed. The watchdog now reaches the API via
`app["voice_watchdog"]` instead, and `tests/test_plugin_context_contract.py`
statically compares the `ctx_kwargs` literal against `PluginContext`'s declared
fields so the next person gets a test failure instead of a silent DJ. That guard
was confirmed to fail when the bug is reintroduced.

**Verification**

- **Voice is back on air.** First voice segment in 41 days went out at
  **2026-07-27 21:34:02** — "We got a little E-Type from the Topp 100…"
- Six TTS generations, every one served by the fallback: `route_speaker=carlin`,
  `fallback=true`, `endpoint=192.168.1.15:11700`. The failover is what is
  carrying the station right now.
- Boot probe logs `TTS ENDPOINT UNREACHABLE (default): localhost:42001` at ERROR
  — the message whose absence cost 41 days.
- `/api/status` → `alerts: []`; `/api/status/health` → per-endpoint
  `{42001: DOWN, 11700: UP}`, watchdog `alerting=false, silent_for=0m,
  fallback_uses=6`.
- Both plugins load, producer live, stream never dropped through three restarts.
- 31 new tests across `test_tts_failover.py`, `test_voice_watchdog.py`,
  `test_plugin_context_contract.py`: routing, speaker substitution,
  duplicate-route collapse, malformed config, failover on connection-refused /
  HTTP 500 / empty body, all-routes-dead, probe semantics, alert-once, reminder
  interval, recovery, the never-succeeded case, and that a crashing watchdog
  can't take the station down.
- Full suite: **71 passed, 6 failed** — the same 6 that fail at `3272adc`
  (verified by stashing). No regressions.

**Side effect of the restart, worth knowing:** the July 4 strict hip-hop seed did
not survive. The producer re-seeded to its default (`character: bob`, no genre
filter, `strict:false`, `hard:false`), so the 23-day genre lock is gone and the
full 7 702-track library is back in rotation — Ulf Lundell and E-Type have
already aired. That also clears bug 1 for now, since the new seed has
`hard:false`; the underlying flag-never-resets defect is still there and will
return with the next `hard:true` seed.

## Follow-up, 2026-07-28

**`qwen-tts` enabled** (by Dan). `enabled` + `active`, `:42001` serving, `Eric`
in its speaker list. Bob is back on his own voice: 11 generations since the
restart, all `route_speaker=Eric, fallback=false`, zero failures. The Chatterbox
failover is now what it should be — a spare, not the thing carrying the station.
The boot probe reports both endpoints reachable.

**Bug 7 fixed** — the script LLM is now re-asked instead of going silent.
`generate_script` attempts up to `script_max_attempts` (default 3) and each retry
tells the model what was wrong with the previous reply rather than repeating the
request blind. Backend exceptions retry too; a transient ollama hiccup also used
to end the cycle. `_parse_response` raises `ScriptParseError` instead of quietly
substituting silence, so "unusable" is distinguishable from "the model chose not
to talk" — a reply with no talk anywhere is retried, but accepted on the final
attempt rather than discarding a valid song order. Malformed segment and voice
entries are skipped individually instead of taking down the whole parse.

Truncation is the likeliest underlying cause — the schema is a flat `segments`
list and `llm_service` sets **no token limit at all** — so `_salvage_json` closes
a cut-off reply after its last complete segment. Recovering nine segments beats
discarding the cycle, and it spares a round-trip. It only runs after a strict
parse has failed, and the scan is bounded. 25 tests; suite at 96 passed, same 6
pre-existing failures.

I could not reproduce the failure on demand — 14 consecutive live calls all
parsed, consistent with the ~6 % rate — so the retry is defensive rather than
targeted at a confirmed mode. The `response` detail in `event_detail` is stored
as `assistant_message[:200]`, a truncated preview, so the historical failures
cannot be diagnosed after the fact. Worth widening if this needs revisiting.

**Hip-hop is back on.** `{"genre":"hip-hop"}` → `pipeline=genre, strict=true,
hard=false`, focus `['hip-hop','hip hop','rap']`, 1 078 of 7 702 tracks matching.
Note `hard=false` this time, so bug 1's mid-song cut every ~50 min stays dormant.

**Seed API staleness — found and fixed.** `POST /api/producer/seed` was returning
200 alongside the *previous* seed: the handler waited for `producer._seed` to be
non-null, which it always already is, so it answered while the new seed was still
queued behind the signal loop. The hip-hop seed landed **42 seconds after** its
own 200 response. Nothing was lost, but a caller could not tell whether its seed
took — and this API's only consumer is an agent.

Rather than poll for a changed `set_at`, `submit_seed` now returns a future that
`_handle_seed` resolves once the seed is genuinely live or has been rejected, so
the answer is attributed to *that* request instead of inferred from a timestamp.
It resolves after the soft flush and before `_build_script`, because phase 2 takes
30-90 s and no caller should be held open for it; phase 1 queues the music a
fraction of a second later. A `finally` guarantees the caller is never left
hanging, and the future carries a result rather than an exception so a
fire-and-forget caller can drop it safely.

The response now says what actually happened:

| field | meaning |
|---|---|
| `applied` | the submitted seed is the live seed |
| `pending` | accepted but still queued — poll `/api/producer/status` |
| `error` | set when interpretation rejected the seed |
| `seed` | the seed **in effect right now**, never an unapplied one |
| `waited_seconds` | how long the request blocked |

`?wait=<seconds>` controls the block (default 20, max 120, `0` returns at once).
`/api/producer/switch` got the same treatment — it was also claiming `ok: true`
before the seed applied. Bad bodies still 400/404 without queueing anything.

Verified against the live station in exactly the scenario that broke: seeding
during the boot build returned `applied: true, waited_seconds: 15.64` with the
*new* hip-hop seed and `script_building: true`. `?wait=0` returned
`pending: true, waited_seconds: 0.0` with the seed still genuinely in effect.
21 tests across both sides of the contract; suite at 117 passed, same 6
pre-existing failures.

**Bug 1 fixed — `hard` is a one-shot again.** The flag was re-read on every script
build, and the producer rebuilds roughly every 50 minutes, so one `{"hard":true}`
seed cut a track mid-song every cycle for as long as it stayed live.

Fixed structurally rather than by resetting state. `_build_script` takes a
keyword-only `apply_hard` that defaults to `False`, and the hard skip moved into
`_maybe_hard_skip()`. Of the four `_build_script` call sites — seed applied,
rolling `buffer_low`, listener-skip reaction, mood tweak — only the seed handler
passes `apply_hard=True`, and it passes `not silent_default` so the boot seed can
never cut a track. The rolling rebuild now *cannot* hard-skip, rather than relying
on a flag having been cleared. `SeedState.hard_consumed` is the second guard and
is surfaced in `/api/producer/status`: `hard` stays the value that was requested,
`hard_consumed` says whether the one-shot has fired. Consumption is marked before
the skip is attempted, so a half-failed skip can't lie in wait.

Verified live: a `{"genre":"hip-hop","hard":true}` seed cut Ulf Lundell straight
into Cypress Hill — one `hard=true → skipped` line — then three non-seed rebuilds
forced via `/api/producer/mood` produced **zero** further skips. 10 tests,
including a 660-cycle simulation of the 23-day case; reverting the fix turns that
into `assert 661 == 1`. A test also asserts that exactly one of the four call sites
passes the flag, so the invariant breaks loudly if a fifth appears. Suite at 127
passed, same 6 pre-existing failures.

## Still to do

1. **`booth.log`**: rotation, and dates on the lines.
3. **Stale SSE tests** — 6 failures pointing at the old `/api/timeline/events`.
4. **Instrumentation drift** — bug 4; `play_count` feeds producer song
   selection, so it's worth knowing which counter to trust.
5. **Widen the stored LLM response** past 200 chars, so the next intermittent
   parse failure can actually be diagnosed.

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
