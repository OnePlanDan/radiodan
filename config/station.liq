# RadioDan - Liquidsoap Station Script
# Mixes background music with TTS announcements and earcons

# =============================================================================
# SETTINGS
# =============================================================================

settings.log.level := 3
settings.server.telnet := true
settings.server.telnet.bind_addr := "0.0.0.0"
settings.server.telnet.port := 1234

# Get config from environment
icecast_password = environment.get("ICECAST_SOURCE_PASSWORD") ?? "hackme"
station_name = environment.get("STATION_NAME") ?? "Radio Dan"

# =============================================================================
# INTERACTIVE CONTROLS (via telnet: var.set/var.get)
# =============================================================================

# Volume controls (0.0 - 1.0)
music_vol = interactive.float("music_vol", 1.0)       # Music volume (0 = muted)
tts_vol = interactive.float("tts_vol", 1.0)           # TTS/voice volume
earcon_vol = interactive.float("earcon_vol", 0.5)     # Earcon/notification volume
duck_amount = interactive.float("duck_amount", 0.15)  # Music level during TTS (0.15 = -16.5 dB)

# Crossfade duration (seconds) — readable by Python bridge for bridge timing math
crossfade_duration = interactive.float("crossfade_duration", 5.0)

# Duck envelope controls — quadratic bezier curve shaping
duck_in_duration = interactive.float("duck_in_duration", 0.8)    # How long duck-in takes (seconds)
duck_out_duration = interactive.float("duck_out_duration", 0.6)  # How long duck-out takes (seconds)
duck_in_curve = interactive.float("duck_in_curve", 0.7)          # Bezier control point (0=ease-in, 1=ease-out)
duck_out_curve = interactive.float("duck_out_curve", 0.3)        # Bezier control point (0=ease-in, 1=ease-out)

# =============================================================================
# SOURCES
# =============================================================================

# Python-controlled music queue (sole track source)
# PlaylistPlanner pushes tracks here via telnet: music_q.push <path>
# All track selection is handled by Python feeder plugins — no Liquidsoap fallback
music_queue = request.queue(id="music_q")

# Safe fallback: silence if Python queue is empty (should not happen in normal operation)
silence = sine(duration=1.0, 440.0)

# Music chain: Python queue is the only track source; silence is the safety net
# track_sensitive=true: wait for current track to finish before switching sources
music_raw = fallback(track_sensitive=true, [music_queue, silence])

# Apply crossfade between tracks
music = cross(duration=crossfade_duration(), fun(a, b) -> begin
  add(normalize=false, [
    fade.out(duration=crossfade_duration(), a.source),
    fade.in(duration=crossfade_duration(), b.source)
  ])
end, music_raw)

# Request queues for TTS and earcons (controlled via telnet)
tts_queue = request.queue(id="tts")
earcon_queue = request.queue(id="earcons")

# =============================================================================
# MIXING
# =============================================================================

# Apply interactive volume controls
# Music volume: controlled by music_vol, ducked to duck_amount when TTS plays
# TTS volume: controlled by tts_vol

# Base music with volume control
music_with_vol = amplify({ music_vol() }, music)

# TTS with volume control
tts_with_vol = amplify({ tts_vol() }, tts_queue)

# Bezier-shaped ducking envelope
# Tracks state transitions and applies quadratic bezier easing:
#   eased = 2*(1-t)*t*cy + t*t  (cy = control point, 0–1)
duck_from = ref(1.0)       # Volume level at start of transition
duck_to = ref(1.0)         # Volume level at end of transition
duck_elapsed = ref(0.0)    # Time elapsed in current transition
prev_ducking = ref(false)  # Previous ducking state
duck_level = ref(1.0)      # Current output level (for tracking)
frame_dur = 1.0 / 25.0     # Approximate frame duration (~40ms)

ducked_music = amplify(
  fun () -> begin
    is_ducking = source.is_ready(tts_queue)
    let target = if is_ducking then duck_amount() else 1.0 end

    # Detect state change → start new transition
    if is_ducking != prev_ducking() then
      duck_from := duck_level()
      duck_to := target
      duck_elapsed := 0.0
      prev_ducking := is_ducking
    end

    let dur = if is_ducking then duck_in_duration() else duck_out_duration() end
    let cy = if is_ducking then duck_in_curve() else duck_out_curve() end
    elapsed = duck_elapsed() + frame_dur
    duck_elapsed := elapsed

    if elapsed >= dur then
      # Transition complete — track target directly (handles duck_amount changes while ducked)
      duck_level := target
      target
    else
      let t = elapsed / dur
      let eased = 2.0 * (1.0 - t) * t * cy + t * t
      let vol = duck_from() + (duck_to() - duck_from()) * eased
      duck_level := vol
      vol
    end
  end,
  music_with_vol
)

# Layer TTS over ducked music using add (both play simultaneously when TTS active)
# This allows music to continue at reduced volume during speech
with_tts = add(normalize=false, [tts_with_vol, ducked_music])

# Add earcons on top (volume controlled via interactive variable)
radio = add(normalize=false, [amplify({ earcon_vol() }, earcon_queue), with_tts])

# Make source infallible (required for output.icecast)
radio = fallback(track_sensitive=false, [radio, blank()])

# =============================================================================
# OUTPUT
# =============================================================================

output.icecast(
  %mp3(bitrate=128),
  host="icecast",
  port=8000,
  password=icecast_password,
  mount="/stream",
  name=station_name,
  description="Ambient AI work companion",
  genre="Ambient",
  radio
)

# =============================================================================
# TELNET COMMANDS
# =============================================================================
#
# === Audio Injection ===
# Inject TTS audio:
#   echo 'tts.push /path/to/audio.wav' | nc localhost 1234
#
# Inject earcon:
#   echo 'earcons.push /sounds/8bit/coin.wav' | nc localhost 1234
#
# Queue music track (PlaylistPlanner):
#   echo 'music_q.push /music/artist/track.mp3' | nc localhost 1234
#
# === Playback Control ===
# Skip current TTS:
#   echo 'tts.skip' | nc localhost 1234
#
# Flush TTS queue (skip all):
#   echo 'tts.flush_and_skip' | nc localhost 1234
#
# Skip to next music track:
#   echo 'music.skip' | nc localhost 1234
#
# === Volume Control ===
# Set music volume (0.0 - 1.0, 0 = muted):
#   echo 'var.set music_vol = 0.5' | nc localhost 1234
#
# Set TTS volume (0.0 - 1.0):
#   echo 'var.set tts_vol = 1.0' | nc localhost 1234
#
# Set duck amount (how much music plays during TTS, 0.0 - 1.0):
#   echo 'var.set duck_amount = 0.3' | nc localhost 1234
#
# Get current volume:
#   echo 'var.get music_vol' | nc localhost 1234
#
# Get crossfade duration:
#   echo 'var.get crossfade_duration' | nc localhost 1234
#
# List available commands:
#   echo 'help' | nc localhost 1234
#
# =============================================================================

# =============================================================================
# CUSTOM TELNET COMMANDS (for stream-aware plugin system)
# =============================================================================

# music.info — returns key=value lines with current track metadata
# Reads from the music source (works with both queue and fallback playlist)
server.register(
  "music.info",
  fun(_) -> begin
    m = music_raw.last_metadata() ?? []
    artist = list.assoc(default="", "artist", m)
    title = list.assoc(default="", "title", m)
    filename = list.assoc(default="", "filename", m)
    genre = list.assoc(default="", "genre", m)
    year = list.assoc(default="", "year", m)
    album = list.assoc(default="", "album", m)
    "artist=#{artist}\ntitle=#{title}\nfilename=#{filename}\ngenre=#{genre}\nyear=#{year}\nalbum=#{album}"
  end
)

# music.remaining — returns seconds remaining in current track
server.register(
  "music.remaining",
  fun(_) -> begin
    r = music_raw.remaining()
    "#{r}"
  end
)

# music.elapsed — returns seconds elapsed in current track
server.register(
  "music.elapsed",
  fun(_) -> begin
    e = music_raw.elapsed()
    "#{e}"
  end
)

# music_q.queue_length — returns number of tracks in the Python-controlled queue
server.register(
  "music_q.queue_length",
  fun(_) -> begin
    q = music_queue.queue()
    "#{list.length(q)}"
  end
)

print("#{station_name} station started!")
print("Telnet control available on port 1234")
print("Stream: http://icecast:8000/stream")
