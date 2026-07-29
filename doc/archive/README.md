# Archive

Superseded documents and artifacts of features that no longer exist. Kept because
they explain how the station got here, moved out of `doc/` because they describe
things that are no longer true.

**Nothing in here should be trusted as a description of the current system.** When
one of these contradicts the code, the code is right.

## What's here

- **`producer-api-handoff.md`** — hand-written API cheatsheet, snapshot 2026-04-17.
  Drifted within weeks: the voice routing and character roster it describes have
  both changed. Replaced by `GET /api/`, which is generated from the live route
  table and so cannot go stale. This file is the reason that endpoint exists.
- **`2026-01-28 Original implementation_plan.md`** — the original build plan.
  Largely delivered, and the architecture has moved on (web UI → JSON API).
- **Timeline / 3D UI prototypes and screenshots** — `radio_timeline_*.html`,
  `2026-02-07-RadioDan3D.html`, `2026-02-06 Screenshot Timeline*.png`,
  `Screenshot from 2026-02-08 *.png`, `Live playlist.png`. The HTML dashboard,
  its templates and its static assets were removed in `3272adc`.
- **`TelegramMenuStyle.png`** — Telegram menu design. Telegram stopped being the
  control channel in `f1feaf7`.

## Convention

Move a doc here rather than deleting it when it is superseded but still explains a
decision. Add a line above saying what replaced it — that pointer is the whole
value of keeping the file.
