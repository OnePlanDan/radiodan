# RadioDan Journal

- 2026-05-08: Shipped 5-layer freeze prevention (Layers 1–5 from `doc/2026-05-08 health-checkup.md`). Layer 1: track-bounded watchdog with 4-strike escalation (skip → known-good fallback → re-push → docker restart) in `bridge/audio/stream_context.py`. Layer 2: `audio.watchdog.fallback_track_path` config wired into producer + watchdog. Layer 3: `LiquidsoapMixer._validate_for_container` walks symlink chains, rejects host-absolute targets unreachable inside the LS container. Layer 4: `silence = blank()` + `track_sensitive=false` on the music fallback in `config/station.liq`. Layer 5a: indexer skips `_damaged/` (`EXCLUDED_TOP_DIRS`). Layer 5b: `scan_mp3_health.sh` uses `ln -srf` for relative symlinks. Radio back live; verified track changes, watchdog armed, validator unit-tested.
- 2026-05-08: Health checkup — Liquidsoap stuck at end of "magi - the stem" since 2026-05-02 08:46 UTC (6 days). Watchdog re-pushes every 10s but rids drop silently; ~261k pushes since freeze. Service still "active", stream still flowing, but no track has changed and no DJ talk has happened in 6 days. Details: `doc/2026-05-08 health-checkup.md`. No changes made.
- 2026-02-10: Fixed ICY metadata one track behind in Winamp — crossfade add() had outgoing track first, Liquidsoap takes metadata from first source in add()
- 2026-02-10: Fixed music directory permissions (700→755) for Liquidsoap container access; added dedicated Samba share with correct masks
- 2026-02-07: Fixed stop/start race condition (SIGKILL fallback, cleanup timeout)
- 2026-02-07: Designed & implemented multi-station presets architecture
- 2026-02-07: Clean-slate push to GitHub (OnePlanDan/radiodan, private)
