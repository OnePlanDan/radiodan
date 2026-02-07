# DJ Claude — Product Requirements Document

> **Goal**: A modular, extensible ambient AI work companion that broadcasts AI activity as internet radio, controlled via pluggable channels.

---

## 1. Core Philosophy

- **Ambient awareness, not multitasking** — Stay loosely connected while living life
- **Modular by design** — Input sources and control channels are pluggable adapters
- **Pragmatic MVP** — Ship working system first, extensibility designed-in but not over-engineered
- **Latency-tolerant** — Delays acceptable; architecture quality over real-time performance

---

## 2. Architecture: Hexagonal (Ports & Adapters)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           INPUT ADAPTERS (Ports)                             │
│   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐                    │
│   │ ClaudeCode   │   │    Email     │   │    Slack     │  ← Future          │
│   │  Adapter ✓   │   │   Adapter    │   │   Adapter    │                    │
│   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘                    │
└──────────┼──────────────────┼──────────────────┼────────────────────────────┘
           │                  │                  │
           ▼                  ▼                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              CORE DOMAIN                                     │
│                                                                              │
│   ┌────────────┐    ┌────────────┐    ┌────────────┐    ┌────────────┐     │
│   │  EventBus  │───▶│ BlockStore │───▶│ TTSService │───▶│ AudioMixer │     │
│   │  (pub/sub) │    │  (SQLite)  │    │            │    │(Liquidsoap)│     │
│   └────────────┘    └────────────┘    └────────────┘    └─────┬──────┘     │
│                                                                │            │
└────────────────────────────────────────────────────────────────┼────────────┘
           │                  │                  │                │
           ▼                  ▼                  ▼                ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         CONTROL ADAPTERS (Ports)                  Icecast   │
│   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐        Stream     │
│   │  Telegram    │   │    WebUI     │   │   Discord    │  ← Future         │
│   │  Channel ✓   │   │   Channel    │   │   Channel    │                    │
│   └──────────────┘   └──────────────┘   └──────────────┘                    │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Key Insight**: Every event carries a `source_id` enabling multi-source routing without architectural changes.

---

## 3. Interface Contracts

### 3.1 InputAdapter (Abstract Base)

```python
class InputAdapter(ABC):
    source_id: str           # "claude-code-1", "email-inbox"
    source_type: str         # "claude_code", "email", "slack"

    async def start() -> None
    async def stop() -> None
    async def send_response(event_id: str, response: str) -> bool
    def is_connected() -> bool
```

### 3.2 ControlChannel (Abstract Base)

```python
class ControlChannel(ABC):
    channel_id: str          # "telegram", "web"

    async def start() -> None
    async def stop() -> None
    async def on_block_created(block: Block) -> None
    async def on_block_playing(block: Block) -> None
    async def on_status_changed(status: SystemStatus) -> None
```

### 3.3 SourceEvent (Canonical Format)

All input adapters emit this format:

```python
@dataclass
class SourceEvent:
    source_id: str           # Which adapter
    source_type: str         # What kind
    event_id: str            # Unique ID
    timestamp: datetime
    event_type: EventType    # tool_start, question, error, etc.
    priority: Priority       # blocking, fyi, done, silent
    content: str             # Full text
    summary: str | None      # Optional pre-summary
    question_options: list[str] | None
    metadata: dict           # Source-specific data
```

---

## 4. MVP Scope

### Build Now ✓

| Component | Description |
|-----------|-------------|
| **ClaudeCodeAdapter** | Single instance via hooks → HTTP POST to bridge |
| **TelegramChannel** | Block menu, question UI, voice input, /tunein |
| **EventBus** | In-process asyncio pub/sub |
| **BlockStore** | SQLite with source_id tracking |
| **TTSService** | Wrapper around local TTS API |
| **AudioMixer** | Liquidsoap control via telnet |
| **Icecast** | Audio streaming server |

### Design For, Build Later ○

| Component | Notes |
|-----------|-------|
| **AdapterRegistry** | Interface ready, MVP uses single adapter |
| **ChannelRegistry** | Interface ready, MVP uses Telegram only |
| **EmailAdapter** | Stub ABC only |
| **SlackAdapter** | Stub ABC only |
| **WebUIChannel** | Stub ABC only |
| **Redis pub/sub** | Swap EventBus impl when needed |

---

## 5. Data Flow

### Event → Audio (happy path)

```
Claude Code hook fires
    ↓
ClaudeCodeAdapter.receive_hook_event()
    → Classify event type & priority
    → Emit SourceEvent to EventBus
    ↓
EventBus publishes "source.event"
    ↓
BlockStore.on_source_event()
    → Persist to SQLite
    → Emit "block.created"
    ↓
Parallel subscribers:
    ├── TTSService → Generate audio → Emit "block.tts_ready"
    ├── TelegramChannel → Update menu UI
    └── AudioMixer → Play earcon immediately
    ↓
AudioMixer.on_tts_ready()
    → Queue or play based on priority
    → Liquidsoap → Icecast stream
```

### User Command → Source

```
User taps [1: SQLite] in Telegram
    ↓
TelegramChannel.on_callback_query()
    → Build UserCommand
    → Publish to EventBus
    ↓
CommandRouter.on_user_command()
    → Look up block.source_id
    → Get adapter from registry
    → adapter.send_response(event_id, "SQLite")
    ↓
ClaudeCodeAdapter delivers to Claude Code stdin
```

---

## 6. File Structure

```
/home/dln/dev/DJClaude/
├── docker-compose.yml           # Icecast + Liquidsoap
├── config/
│   ├── station.liq              # Liquidsoap DJ logic
│   ├── icecast.xml              # Icecast config
│   └── djclaude.yaml            # App configuration
│
├── bridge/                      # Core Python application
│   ├── main.py                  # Entry point
│   ├── config.py                # Config loading
│   ├── event_bus.py             # Pub/sub
│   │
│   ├── models/
│   │   ├── events.py            # SourceEvent, UserCommand
│   │   └── blocks.py            # Block dataclass
│   │
│   ├── store/
│   │   └── block_store.py       # SQLite persistence
│   │
│   ├── services/
│   │   ├── tts_service.py       # TTS generation
│   │   ├── stt_service.py       # Whisper transcription
│   │   └── summarizer.py        # Ollama summarization
│   │
│   ├── audio/
│   │   ├── mixer.py             # Liquidsoap telnet control
│   │   └── play_queue.py        # Playback queue logic
│   │
│   ├── adapters/                # INPUT PORTS
│   │   ├── base.py              # InputAdapter ABC
│   │   ├── claude_code.py       # MVP implementation
│   │   ├── email.py             # Future: stub only
│   │   └── slack.py             # Future: stub only
│   │
│   └── channels/                # OUTPUT PORTS
│       ├── base.py              # ControlChannel ABC
│       ├── telegram.py          # MVP implementation
│       └── web.py               # Future: stub only
│
├── hooks/                       # Claude Code integration
│   └── on_output.py             # Hook → HTTP POST
│
├── sounds/8bit/                 # Earcons (11 sounds)
└── music/                       # Background music + TTS cache
```

---

## 7. External Services (Your Setup)

### TTS API (port 7860)

Rich TTS API with multiple voice modes. For DJ Claude, we'll use `/api/tts/custom`:

```bash
curl -X POST http://localhost:7860/api/tts/custom \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Claude has finished running tests.",
    "language": "English",
    "speaker": "vivian",
    "instruct": "Speak calmly and clearly",
    "temperature": 0.9,
    "max_new_tokens": 2048
  }'
# Returns: audio/wav
```

**Available speakers**: serena, vivian, uncle_fu, ryan, aiden, ono_anna, sohee, eric, dylan

### Whisper/STT API (port 5000)

OpenAI-compatible transcription endpoint:

```bash
curl -X POST http://127.0.0.1:5000/v1/audio/transcriptions \
  -F "file=@voice_message.ogg"
# Returns: JSON with transcription
```

---

## 8. Configuration

```yaml
# config/djclaude.yaml
server:
  host: 0.0.0.0
  port: 8080

audio:
  icecast:
    host: localhost
    port: 8000
    mount: /stream
  liquidsoap:
    telnet_port: 1234

  tts:
    endpoint: http://localhost:7860/api/tts/custom
    speaker: vivian              # Default voice
    language: English
    instruct: "Speak calmly and clearly"
    temperature: 0.9

  stt:
    endpoint: http://127.0.0.1:5000/v1/audio/transcriptions

sources:
  - id: claude-code-main
    type: claude_code
    enabled: true

channels:
  telegram:
    enabled: true
    token: ${TELEGRAM_BOT_TOKEN}
    allowed_users: [${TELEGRAM_USER_ID}]

sounds:
  theme: 8bit
```

---

## 9. Multi-Source Vision (Future)

The architecture supports this scenario without changes:

```yaml
sources:
  - id: claude-code-main
    type: claude_code
  - id: claude-code-docs
    type: claude_code
  - id: work-email
    type: email
    imap_server: imap.gmail.com
```

User goes hiking for 5 hours. All three sources emit events tagged with their `source_id`. Questions route back to the correct source. Catch-up summary groups by source.

---

## 10. Implementation Phases

### Phase 1: Audio Infrastructure
- [ ] Install Icecast2 + Liquidsoap (Docker)
- [ ] Configure station.liq with music ducking
- [ ] Verify: Phone connects, hears music

### Phase 2: Core Bridge
- [ ] Create project structure (bridge/, models/, etc.)
- [ ] Implement EventBus (asyncio pub/sub)
- [ ] Implement BlockStore (SQLite)
- [ ] Define InputAdapter and ControlChannel ABCs

### Phase 3: Telegram Channel
- [ ] Implement TelegramChannel
- [ ] Block navigation menu (F/S buttons)
- [ ] Voice message → Whisper → response
- [ ] /tunein command

### Phase 4: Claude Code Integration
- [ ] Implement ClaudeCodeAdapter
- [ ] Write Claude Code hook (on_output.py)
- [ ] Wire answer routing back to stdin

### Phase 5: Audio Pipeline
- [ ] Integrate TTS service
- [ ] Implement AudioMixer (Liquidsoap telnet)
- [ ] Add earcons (11 8-bit sounds)
- [ ] Test full flow: Claude output → TTS → stream

### Phase 6: Polish
- [ ] "While You Were Away" catch-up flow
- [ ] DND mode
- [ ] Error handling & logging
- [ ] Ship it 🚀

---

## 11. Verification Plan

After each phase, verify:

1. **Phase 1**: `curl http://localhost:8000/stream` returns audio
2. **Phase 2**: Unit tests for EventBus and BlockStore
3. **Phase 3**: Send Telegram command, see response
4. **Phase 4**: Claude Code outputs appear in block store
5. **Phase 5**: Hear TTS in audio stream when Claude outputs
6. **Phase 6**: Full end-to-end: Start task → go away → catch up

---

## 12. User Action Required

Before implementation begins:

| Action | Status | Notes |
|--------|--------|-------|
| TTS API available | ✅ | Port 7860, speaker "vivian" |
| Whisper API available | ✅ | Port 5000, OpenAI-compatible |
| Create Telegram bot | ⏳ | Message @BotFather → /newbot → save token |
| Get Telegram user ID | ⏳ | Message @userinfobot to get your numeric ID |
| Create 8-bit earcons | ⏳ | Use bfxr.net (11 sounds) |
| Open Icecast port | ⏳ | Port 8000 (firewall/router if external) |

---

## 13. Design Considerations & Notes

### TTS Strategy: Just-in-Time Preloading

**Do NOT pre-generate all blocks.** Instead:
- Generate TTS for the *current* block
- While streaming, generate 2-3 blocks ahead (rolling buffer)
- Generation is faster than playback, so this stays ahead naturally
- No backlog accumulation, no wasted computation for blocks user might skip

```
Playing: block_5.wav
Buffer:  [block_6.wav ✓] [block_7.wav ✓] [block_8 generating...]
```

### Voice Chattiness: Tiered Fallback Plan

If full TTS proves too interruptive (unproven territory), degrade gracefully:

| Tier | Approach | When to Use |
|------|----------|-------------|
| **Tier 1** | Full TTS for all blocks | Default, try first |
| **Tier 2** | Summary TTS only | If Tier 1 feels like a podcast |
| **Tier 3** | Sidecar LLM batch summary | For long absences: let Claude run to completion/question, then summarize entire session via Ollama |

This can be user-toggled or auto-detected based on block velocity.

### Liquidsoap Complexity

Liquidsoap is powerful but notoriously arcane to debug. Mitigation strategies:
- Keep `station.liq` as simple as possible
- Log liberally
- Have fallback: if Liquidsoap fails, direct FFmpeg mixing as backup
- Test the telnet control interface thoroughly before building on it

### Why Modular Architecture

Even for single-user, the adapter/channel abstraction:
- Makes testing easier (mock adapters)
- Prevents "Telegram code everywhere" sprawl
- Enables future experimentation (try Discord? Web UI?) without rewrites
- Cost is ~200 lines of interfaces, not runtime overhead

---

*Generated: 2026-01-28*
