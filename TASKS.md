# RadioDan - Task Breakdown

## Responsibilities

### Claude Does (on this server)

| Task | Status | Notes |
|------|--------|-------|
| Install Icecast2 | ⏳ | `apt install icecast2` |
| Install Liquidsoap | ⏳ | Audio mixing engine |
| Write Bridge Service | ⏳ | Python - block parser, orchestration |
| Write Telegram Bot | ⏳ | Python - needs YOUR token first |
| Write Claude Code hooks | ⏳ | Shell/Python |
| Configure Icecast | ⏳ | icecast.xml |
| Configure Liquidsoap | ⏳ | .liq scripts |
| Set up block store | ⏳ | SQLite |
| Integrate your TTS API | ⏳ | Need endpoint details |
| Integrate your Whisper API | ⏳ | Need endpoint details |

### User Does

| Task | Status | Notes |
|------|--------|-------|
| Create Telegram Bot | ⏳ | Message @BotFather on Telegram |
| Provide bot token | ⏳ | From BotFather |
| Provide Telegram user ID | ⏳ | Your numeric ID |
| Create 8-bit earcons | ⏳ | bfxr.net (11 sounds) |
| Open Icecast port | ⏳ | Firewall/router if needed |
| Test from phone | ⏳ | Audio player + Telegram |
| Provide TTS API details | ⏳ | How to call it |
| Provide Whisper API details | ⏳ | How to call it |

---

## Questions Pending

### 1. TTS API - How does it work?

```bash
# HTTP endpoint?
curl http://localhost:????/tts -d "text=hello" -o output.wav

# Command line tool?
tts --text "hello" --out output.wav

# Other?
```

**Answer:** _________________

### 2. Whisper API - How does it work?

```bash
# HTTP endpoint?
curl http://localhost:????/transcribe -F "audio=@input.ogg"

# Command line?
whisper input.ogg --model small

# Other?
```

**Answer:** _________________

### 3. Icecast Port

Default is 8000. OK to use? Can you expose it externally?

**Answer:** _________________

### 4. Project Location

Building in `/home/dln/dev/DJClaude/` - correct?

**Answer:** _________________

---

## Build Phases

### Phase 1: Infrastructure
```
[ ] User: Create Telegram bot → get token + user ID
[ ] Claude: Install Icecast2 + Liquidsoap
[ ] Claude: Get basic audio stream working (music only)
[ ] Checkpoint: User connects phone, hears music
```

### Phase 2: Telegram Bot
```
[ ] Claude: Write bot with user's token
[ ] Claude: Basic menu working (buttons appear)
[ ] Checkpoint: User taps buttons, bot responds
```

### Phase 3: Bridge + Blocks
```
[ ] Claude: Write block parser
[ ] Claude: Connect to TTS API
[ ] Claude: Audio mixing (TTS into stream)
[ ] Checkpoint: User hears voice in stream
```

### Phase 4: Claude Code Integration
```
[ ] Claude: Write hooks
[ ] Claude: Wire everything together
[ ] Checkpoint: Full flow works
```

### Phase 5: Polish
```
[ ] User: Create 8-bit sounds in bfxr
[ ] Claude: Integrate earcons
[ ] Ship it 🚀
```

---

*Last updated: 2026-01-28*
