# DJ Claude

> Stay loosely connected to your AI work while living your life. No need to sit at the terminal - start a task, step away, and check in when it suits you.

**Tagline:** *"Vibe with DJ Claude"*

## Vision

This is about **freedom from the terminal** - not cramming more productivity into every moment, but letting life continue while AI work continues.

You start Claude on a task. Then you go hiking, hit the gym, cook dinner, solder a flux capacitor, clean the house - whatever. Claude works in the background. You're **loosely connected** through ambient audio, like having a colleague working in another room.

- Check in when *you* want, not when the computer demands it
- If Claude needs you, it'll wait - no pressure
- Catch up on progress at your own pace
- Answer questions with a quick tap or voice message

**This is ambient awareness, not multitasking.**

---

## The DJ Metaphor

The system works like a DJ mixing a live set:

| DJ Concept | DJ Claude Equivalent |
|------------|---------------------|
| Instrumental track | Background music (continuous) |
| Vocal drops | TTS announcements |
| Sound effects / samples | Earcons (tick, tock, riffs) |
| DJ talking to crowd | Claude asking questions |
| Crowd requests | Your voice input |
| Reading the room | Priority levels (when to interrupt) |
| The mix | Everything blended into one stream |

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         USER'S PHONE                            │
│                                                                 │
│   ┌─────────────────┐          ┌─────────────────────────────┐  │
│   │  Audio Player   │          │  Telegram                   │  │
│   │  (any app)      │          │                             │  │
│   │                 │          │  ┌───────────────────────┐  │  │
│   │  🎵 Music +     │          │  │       [  ...  ]       │  │  │
│   │     DJ Claude   │          │  ├───────────┬───────────┤  │  │
│   │                 │          │  │ [F] 🟢   │    [S]    │  │  │
│   │  ▶━━━━━━━━━━━━  │          │  ├───────────┼───────────┤  │  │
│   │                 │          │  │   [F]     │    [S]    │  │  │
│   │                 │          │  ├───────────┼───────────┤  │  │
│   └─────────────────┘          │  │   [F]     │    [S]    │  │  │
│                                │  ├───────────┼───────────┤  │  │
│                                │  │   [F]     │    [S]    │  │  │
│                                │  ├───────────┴───────────┤  │  │
│                                │  │       [  ...  ]       │  │  │
│                                │  └───────────────────────┘  │  │
│                                │                             │  │
│                                │  🎤 [Voice Message Input]   │  │
│                                └─────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                    │                         │
                    │ Icecast                 │ Telegram Bot API
                    │ (port 8000)             │
                    ▼                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                       REMOTE SERVER                             │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                    Bridge Service                        │   │
│  │                                                          │   │
│  │  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐  │   │
│  │  │   Block     │    │    TTS      │    │   Audio     │  │   │
│  │  │   Store     │    │   Queue     │    │   Mixer     │  │   │
│  │  │             │    │             │    │             │  │   │
│  │  │ [block_0]   │───▶│ Full→.wav   │───▶│ Music.mp3   │  │   │
│  │  │ [block_1]   │    │ Sum→.wav    │    │ + Earcons   │  │   │
│  │  │ [block_2]   │    │ + earcons   │    │ + TTS       │  │   │
│  │  │ [block_3]   │    │             │    │ = Stream    │──────▶ Icecast
│  │  │   ...       │    │             │    │             │  │   │
│  │  └─────────────┘    └─────────────┘    └─────────────┘  │   │
│  │         │                                    ▲          │   │
│  │         │                                    │          │   │
│  │         ▼                                    │          │   │
│  │  ┌─────────────┐                    ┌────────┴────────┐ │   │
│  │  │  Telegram   │                    │   Play Queue    │ │   │
│  │  │    Bot      │───────────────────▶│                 │ │   │
│  │  │             │  "user tapped F2"  │ current: blk_1  │ │   │
│  │  │ Send menu   │                    │ next: [blk_2_f] │ │   │
│  │  │ updates     │                    └─────────────────┘ │   │
│  │  └─────────────┘                                        │   │
│  │         ▲                                               │   │
│  └─────────│───────────────────────────────────────────────┘   │
│            │                                                    │
│  ┌─────────┴─────────┐                                         │
│  │   Claude Code     │                                         │
│  │                   │                                         │
│  │  hook: on_output  │─────▶ New block created                 │
│  │                   │                                         │
│  │  receives answers │◀───── From Telegram (voice/button)      │
│  └───────────────────┘                                         │
└─────────────────────────────────────────────────────────────────┘
```

---

## Earcons (Audio Signatures)

Short, distinctive sounds that convey meaning instantly - no words needed. Like learning notification sounds, you internalize them quickly.

### ⚠️ Discipline Rule: Avoid the Earcon Hole

Sound design is an infinite rabbit hole. To ship, we need constraints:

1. **Default theme: 8-bit** - First and only theme for MVP
2. **Defaults must be excellent** - No "user can change it" excuse for poor choices
3. **Minimal set first** - Only the sounds in the Sound Map below, nothing more
4. **Lock and ship** - Resist tweaking. Good enough that works > perfect that doesn't exist

**Why 8-bit:**
- Distinct from real-world sounds (won't confuse with notifications)
- Short and punchy (sub-second)
- Clear emotional valence (success = happy, failure = sad)
- Nostalgic/fun vibe fits "DJ Claude"
- Free tools available (bfxr, Chiptone)

**Tools:**
- **bfxr** (browser): https://www.bfxr.net/
- **Chiptone** (browser): https://sfbgames.itch.io/chiptone
- **sfxr** (standalone): original by DrPetter

**Design Principles:**
- **Tick/Tock** for routine tool use - minimal, rhythmic, doesn't interrupt flow
- **Music samples** for big events - test crash, build success, task complete
- **Rising/falling tones** for state changes - question pending, answered
- **Silence is information** - no sound = Claude working normally
- **Customizable** - pick your own samples (metal? classical? 8-bit?)

**Sound Map (8-bit theme, MVP set):**

```
┌─────────────────┬───────────────────────────────────────────┐
│  Event          │  8-bit Sound                              │
├─────────────────┼───────────────────────────────────────────┤
│  Tool start     │  tick (short blip)                        │
│  Tool complete  │  tock (slightly lower blip)               │
│  Test pass      │  ✨ coin/powerup (bright, major)          │
│  Test fail      │  💀 damage/hit (descending, minor)        │
│  Build success  │  🎮 level-up fanfare (short, triumphant)  │
│  Build fail     │  ☠️ game-over tone (descending wah)       │
│  Error          │  ⚡ warning beep (attention-getting)      │
│  Question       │  ❓ rising arpeggio (anticipation)        │
│  Answer received│  ✓ confirmation blip (resolved)           │
│  Task complete  │  🏆 victory jingle (2-3 sec max)          │
│  Waiting/idle   │  silence (music only)                     │
└─────────────────┴───────────────────────────────────────────┘

Total: 11 sounds. That's the MVP set. No more until v2.
```

**Example Flow (What You'd Hear):**

```
🎵 [music playing]

*blip* .......................... tool start
*bloop* ......................... tool complete
*blip*
*bloop*
*blip*
"Running tests..." .............. TTS - important moment
*bloop*
🎮 [coin sound!] ................ tests passed!

🎵 [music continues]

*blip*
*bloop*
🎵↗️ [rising arpeggio] ........... question incoming
"Which API endpoint? Option 1: REST. Option 2: GraphQL."

🎵 [music continues, waiting...]

[you tap "1" on Telegram]

✓ [confirmation blip] ........... answer received
*blip* .......................... Claude continues
```

---

## Priority Levels

Not all events need the same attention. The system knows when to interrupt vs. stay ambient.

| Priority | Icon | Meaning | Audio Behavior |
|----------|------|---------|----------------|
| 🔴 Blocking | ❓ | Claude needs answer to continue | TTS + repeat until acknowledged |
| 🟡 FYI | 📝 | Progress update, no action needed | Earcon only, or brief TTS |
| 🟢 Done | ✅ | Task complete, review when ready | Triumphant earcon + optional TTS |
| ⚫ Silent | 💭 | Routine work | Tick/tock only |

**Do Not Disturb Mode:** Sometimes you just want the music. Toggle off all TTS, check Telegram manually when ready.

---

## "While You Were Away" (Catch-up)

When you tune back in after being away:

```
┌─────────────────────────────────────────────────────┐
│  📋 While you were away:                            │
│                                                     │
│  • Edited 3 files (user.py, auth.py, config.py)    │
│  • Ran tests: 47 passed, 0 failed                  │
│  • Build: successful                                │
│  • ❓ 1 question waiting (database choice)          │
│                                                     │
│  [🔊 Play Summary]  [📜 Show Blocks]  [⏭️ Skip]    │
└─────────────────────────────────────────────────────┘
```

Options:
- **Play Summary** - Condensed TTS of what happened
- **Show Blocks** - Browse the full block menu
- **Skip** - Jump to current state, ignore history

---

## Telegram Interface

### Block Navigation Menu

```
┌─────────────────────┐
│      [  ...  ]      │  ← Scroll up (older blocks)
├──────────┬──────────┤
│   [F]    │   [S]    │  ← Block 1 (Full / Summary)
│  🟢      │          │     ↑ Currently playing
├──────────┼──────────┤
│   [F]    │   [S]    │  ← Block 2
├──────────┼──────────┤
│   [F]    │   [S]    │  ← Block 3
├──────────┼──────────┤
│   [F]    │   [S]    │  ← Block 4
├──────────┴──────────┤
│      [  ...  ]      │  ← Scroll down (newer blocks)
└─────────────────────┘
```

- **[F]** = Play full version of block
- **[S]** = Play summary version
- **🟢** = Currently playing indicator
- **[...]** = Scroll through block history

### Question Block (Special UI)

```
┌───────────────────────────────────────┐
│           [  ...  ]                   │
├───────────┬───────────────────────────┤
│   [F]     │    [S]                    │
├───────────┴───────────────────────────┤
│  ❓ Which database should I use?      │
├───────────┬───────────┬───────────────┤
│ [1:SQLite]│[2:Postgres]│ [3:MongoDB] │
├───────────┴───────────┼───────────────┤
│         [4: 🎤 Speak Response]        │
├───────────────────────────────────────┤
│           [  ...  ]                   │
└───────────────────────────────────────┘
```

### User Actions

| Action | Result |
|--------|--------|
| Tap `[F]` on block N | Queue full TTS, update 🟢 indicator |
| Tap `[S]` on block N | Queue summary TTS, update 🟢 indicator |
| Tap `[...]` up/down | Scroll through block history |
| Send voice message | Whisper → text → Claude Code input |
| Tap answer button | Send answer to Claude Code |

---

## Components

### 1. Block Store

Every Claude Code output becomes a block:

```python
Block {
    id: int
    type: "tool" | "thinking" | "text" | "question" | "error"
    priority: "blocking" | "fyi" | "done" | "silent"
    timestamp: datetime
    content: str           # Full content
    summary: str           # Auto-generated summary (via local LLM)
    tts_full: path         # Path to full TTS audio
    tts_summary: path      # Path to summary TTS audio
    earcon: str            # Which earcon to play
    played: bool           # Has user heard this?
}
```

### 2. Audio Mixer

Blends three sources into one Icecast stream:

```
┌─────────────┐
│   Music     │────┐
│  (continuous)    │
└─────────────┘    │     ┌─────────────┐
                   ├────▶│   Mixer     │────▶ Icecast
┌─────────────┐    │     │  (ffmpeg/   │      stream
│   Earcons   │────┤     │ liquidsoap) │
│  (samples)  │    │     └─────────────┘
└─────────────┘    │
                   │
┌─────────────┐    │
│    TTS      │────┘
│  (voice)    │
└─────────────┘
```

- Music plays continuously
- Earcons drop in at events
- TTS ducks the music slightly, then restores

### 3. Telegram Bot

- Receives voice messages → Whisper transcription → Claude Code
- Sends menu updates when new blocks arrive
- Handles button taps for navigation and answers
- Manages "While you were away" catch-up flow

### 4. Claude Code Hook

Intercepts Claude Code output and feeds it to the Bridge Service:

```bash
# Hook triggers on output
on_output() {
    # Parse output into block
    # Send to Bridge Service API
    # Bridge handles TTS, earcons, Telegram update
}
```

---

## Technology Stack

| Component | Technology |
|-----------|------------|
| Telegram Bot | python-telegram-bot / Telethon |
| Audio Streaming | Icecast2 |
| Audio Mixing | FFmpeg / Liquidsoap |
| TTS | Local API (your setup) |
| STT | Whisper (local) |
| Summarization | Ollama (local LLM) |
| Block Store | SQLite / JSON files |
| Claude Code Hook | Python script |

---

## Open Questions

1. **Music source** - Your playlist? Ambient generator? User configurable?
2. **Earcon library** - Start with basics (tick/tock/chimes) or go full sample pack?
3. **Multiple sessions** - One bot per Claude instance, or multiplexed?
4. **Persistence** - Keep blocks across sessions?
5. **Mobile app vs Telegram** - Telegram for MVP, custom app later?

---

## References

- Telegram Menu Sketch: `TelegramMenuStyle.png`

---

*Project: DJ Claude*
*Created: 2026-01-28*
