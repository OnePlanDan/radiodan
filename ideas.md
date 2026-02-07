Here is the project blueprint. Save this as `README.md` to start your repository.

***

# Agent Radio: The Headless AI Interface

**Concept:** A "VPN for your attention." This system runs an AI agent (Claude Code/Gemini) on a remote server, broadcasts its activity as a continuous internet radio stream (mixed with Lo-Fi music), and allows control via a Telegram bot.

**Status:** `Prototype Phase`
**Date:** January 28, 2026

---

## 🏗 Architecture

```mermaid
graph TD
    subgraph "Remote Server (The Studio)"
        A[Agent CLI] -->|stdout| B[Python Bridge]
        B -->|Text| C[Local TTS Engine]
        C -->|WAV Files| D[Liquidsoap DJ]
        E[Music Folder] -->|MP3s| D
        D -->|Mixed Stream| F[Icecast Server]
    end

    subgraph "Control Plane"
        B -->|State Updates| G[Telegram Bot]
        G -->|Voice Input/Buttons| B
    end

    subgraph "User (The Car)"
        F -->|Audio Stream| H[Phone (VLC)]
        G <-->|Control UI| I[Phone (Telegram)]
    end
```

---

## 🛠 Step 1: The Broadcast Tower
*Hosting the Icecast server and the Liquidsoap mixer.*

**File:** `docker-compose.yml`
```yaml
version: '3.8'

services:
  icecast:
    image: infiniteproject/icecast
    ports:
      - "8000:8000"
    environment:
      - ICECAST_SOURCE_PASSWORD=hackme
      - ICECAST_ADMIN_PASSWORD=hackme
      - ICECAST_PASSWORD=hackme
      - ICECAST_RELAY_PASSWORD=hackme
    networks:
      - radio-net

  liquidsoap:
    image: savonet/liquidsoap:v2.2.3
    volumes:
      - ./config:/etc/liquidsoap
      - ./music:/music  # Shared volume for MP3s and generated TTS
    command: liquidsoap /etc/liquidsoap/station.liq
    depends_on:
      - icecast
    networks:
      - radio-net
    ports:
      - "1234:1234" # Telnet control port

networks:
  radio-net:
```

**File:** `config/station.liq`
*The "DJ" logic that handles music ducking when the Agent speaks.*
```ruby
# /etc/liquidsoap/station.liq

# 1. Setup Environment
settings.telnet.bind_addr.set("0.0.0.0")
settings.telnet.port.set(1234)

# 2. Input Sources
# A. The Agent's Voice Queue (High Priority)
agent_voice = request.queue(id="agent_q")

# B. Background Music (Safe Fallback)
# Maps to ./music inside the container
background_music = playlist(mode="randomize", reload_mode="watch", "/music")

# C. Emergency Silence (Sine wave if files missing)
security = mksafe(single("sine:440"))

# 3. Mixing Logic
# Duck music volume to 20% when Agent speaks, fade back after 0.5s
radio = smooth_add(delay=0.5, p=0.2, normal=agent_voice, special=background_music)
radio = fallback(track_sensitive=false, [radio, security])

# 4. Broadcast to Icecast
output.icecast(%mp3(bitrate=128),
  host="icecast", port=8000, password="hackme",
  mount="stream", name="Agent Radio", description="AI Dev Companion",
  radio)
```

---

## 🔌 Step 2: The Studio (Python Bridge)
*Wraps the Agent CLI, captures text, generates TTS, and injects it into the radio.*

**File:** `studio.py`
```python
import subprocess
import socket
import os
import time
from pathlib import Path

# Configuration
LIQUIDSOAP_HOST = "localhost"
LIQUIDSOAP_PORT = 1234
# Ensure this path maps to the './music' volume in Docker
TTS_OUTPUT_DIR = "./music/tts_cache" 
AGENT_COMMAND = ["ping", "google.com"] # Replace with ["claude", "code"]

class RadioDJ:
    """Controls the Liquidsoap instance via Telnet"""
    def push_to_queue(self, filename):
        # Liquidsoap sees path from INSIDE container
        docker_path = f"/music/tts_cache/{os.path.basename(filename)}"
        
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect((LIQUIDSOAP_HOST, LIQUIDSOAP_PORT))
                cmd = f"agent_q.push {docker_path}\n"
                s.sendall(cmd.encode('utf-8'))
                print(f"📻 Queued: {docker_path}")
        except Exception as e:
            print(f"❌ Radio Error: {e}")

class Narrator:
    """Converts text to speech"""
    def speak(self, text, block_id):
        print(f"🗣️ TTS: {text.strip()}...")
        filename = f"{TTS_OUTPUT_DIR}/block_{block_id}.wav"
        
        # PROTOTYPE: Using Mac 'say' for testing. 
        # PROD: Replace with OpenAI TTS / ElevenLabs / Coqui
        subprocess.run(["say", "-o", filename, "--data-format=LEF32@22050", text])
        return filename

class AgentWatcher:
    """Runs the CLI and captures output"""
    def __init__(self, dj, narrator):
        self.dj = dj
        self.narrator = narrator
        self.block_counter = 0

    def start(self):
        # bufsize=0 and universal_newlines=True for real-time capture
        process = subprocess.Popen(
            AGENT_COMMAND,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            text=True,
            bufsize=1 
        )
        print(f"🚀 Agent started: {' '.join(AGENT_COMMAND)}")

        while True:
            line = process.stdout.readline()
            if not line: break
            if line.strip():
                self.process_block(line)

    def process_block(self, text):
        self.block_counter += 1
        # 1. (Future) Send "Green Dot" update to Telegram
        # 2. Generate Audio
        audio_file = self.narrator.speak(text, self.block_counter)
        # 3. Broadcast
        self.dj.push_to_queue(audio_file)

if __name__ == "__main__":
    # Setup
    Path(TTS_OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    
    dj = RadioDJ()
    narrator = Narrator()
    watcher = AgentWatcher(dj, narrator)
    
    watcher.start()
```

---

## 🎧 Step 3: The Receiver (Client)

### How to Listen (Screen Off / Car Mode)
Do not use a web browser. Use **VLC**.

1.  **Install VLC** on your phone (iOS/Android).
2.  **Open VLC** -> Network Stream.
3.  **Enter URL:** `http://YOUR_SERVER_IP:8000/stream`
4.  **Profit:** The audio will persist even when the screen locks.

### The "One-Click" Setup
Create a file named `radio.m3u` and send it to your Saved Messages in Telegram:
```text
#EXTM3U
#EXTINF:-1, Agent Radio
http://YOUR_SERVER_IP:8000/stream
```
*Tap this file in Telegram -> Open in VLC.*

---

## 🔮 Roadmap

1.  **The Accumulator:** Logic to buffer CLI lines into semantic "Blocks" (Thought, Tool Use, Question).
2.  **The Summary Engine:** Pass Blocks to Ollama to generate 1-sentence summaries for the radio stream.
3.  **Telegram Control:** Implement the "Menu Scrubber" (Green Dot) UI.
4.  **`/tunein` Command:** Bot sends a `station.m3u` file so users never type the IP.
    - User sends `/tunein` to the bot
    - Bot replies with a tiny `.m3u` file attachment
    - User taps it → "Open with...?" → VLC
    - Stream starts. Done.