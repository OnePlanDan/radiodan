# RadioDan Journal

- 2026-02-10: Fixed ICY metadata one track behind in Winamp — crossfade add() had outgoing track first, Liquidsoap takes metadata from first source in add()
- 2026-02-10: Fixed music directory permissions (700→755) for Liquidsoap container access; added dedicated Samba share with correct masks
- 2026-02-07: Fixed stop/start race condition (SIGKILL fallback, cleanup timeout)
- 2026-02-07: Designed & implemented multi-station presets architecture
- 2026-02-07: Clean-slate push to GitHub (OnePlanDan/radiodan, private)
