# RadioDan

AI-powered internet radio station. Python bridge controls Liquidsoap via telnet, streams to Icecast. DJ presenter generates voice segments with LLM + TTS. Controllable via Telegram bot and web GUI.

## Quick Start

```bash
# Clone
git clone https://github.com/OnePlanDan/radiodan.git
cd radiodan

# Pick a station preset
echo radio-dan > .station

# Configure secrets
cp stations/radio-dan/.env.example stations/radio-dan/.env
nano stations/radio-dan/.env    # fill in tokens + passwords

# Add music
mkdir -p music                  # drop .mp3/.ogg/.flac files here

# Start
./run_radiodan.sh start
```

Stream URL shown at startup: `http://<your-ip>:49994/stream`

## Requirements

- Docker + Docker Compose
- Python 3.11+ with [uv](https://astral.sh/uv)
- Telegram bot token (from [@BotFather](https://t.me/BotFather))

## Commands

```
./run_radiodan.sh status           # what's running
./run_radiodan.sh start            # start everything
./run_radiodan.sh stop             # stop everything
./run_radiodan.sh restart          # restart all services
./run_radiodan.sh restart-pyhost   # restart Python bridge only
./run_radiodan.sh stations         # list station presets
./run_radiodan.sh logs             # tail all logs
./run_radiodan.sh bot              # run bot in foreground (debug)
```

## Station Presets

Each station is a directory under `stations/` with its own identity:

```
stations/
  radio-dan/
    station.yaml    # name, DJ persona, plugins, prompts
    .env            # secrets (gitignored)
    .env.example    # template
```

Select a station: `STATION=radio-dan ./run_radiodan.sh start`
Or set a default: `echo radio-dan > .station`

New station = copy a directory, change the personality.

## Architecture

```
Telegram / Web GUI
       |
  Python Bridge (bridge/)
       |
  Liquidsoap (telnet:1234) --> Icecast (:49994) --> Listeners
       |
  music/ + TTS audio
```

- **Plugin system**: `@register_plugin` decorator, multi-instance via SQLite
- **Voice pipeline**: Plugin -> LLM -> TTS -> crossfade mixer
- **Playlist planner**: lookahead queue, history tracking, feeder plugins
- **Event timeline**: SSE-streamed to web GUI for live monitoring
