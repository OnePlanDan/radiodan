The Networking Problem

  For streaming audio, listeners need to connect to Icecast. At home you port-forward. At work, the firewall blocks that. Three options:

  Tailscale (my strong recommendation)
  - Install on both servers + your phone + laptop
  - Creates a private mesh VPN that punches through firewalls using outbound connections only
  - Each device gets a stable IP like 100.x.y.z
  - Your phone streams from 100.x.y.z:49994 — works from anywhere
  - Free for personal use, takes 5 minutes to set up
  - Corporate firewall never even sees it — it's just an outbound HTTPS connection

  Cloudflare Tunnel — gives you a public https://work-radio.yourdomain.com URL, also outbound-only. More setup, needs a domain.

  SSH reverse tunnel — work server tunnels back to home server. Fragile, needs babysitting.

  The Full Picture

  ┌─────────────────────────────────────────────────┐
  │  Private Git Repo (your server / Forgejo)       │
  │                                                 │
  │  stations/                                      │
  │    home-chill/     ← ambient DJ persona         │
  │      station.yaml                               │
  │      .env                                       │
  │    work-beats/     ← upbeat office persona      │
  │      station.yaml                               │
  │      .env                                       │
  │  bridge/           ← shared code                │
  │  config/           ← shared liquidsoap/icecast  │
  │  run_radiodan.sh   ← STATION=xxx ./run...       │
  └──────────┬──────────────────┬────────────────────┘
             │ git pull         │ git pull
             ▼                  ▼
    ┌─────────────────┐  ┌──────────────────┐
    │  Home Server    │  │  Work Server     │
    │  STATION=       │  │  STATION=        │
    │   home-chill    │  │   work-beats     │
    │                 │  │                  │
    │  Icecast:49994  │  │  Icecast:49994   │
    │  Web UI:49995   │  │  Web UI:49995    │
    └────────┬────────┘  └────────┬─────────┘
             │                    │
             │   Tailscale mesh   │
             │  ◄────────────────►│
             │                    │
        ┌────┴────────────────────┴────┐
        │  📱 Your Phone / Laptop      │
        │  home:  100.64.1.1:49994     │
        │  work:  100.64.1.2:49994     │
        └──────────────────────────────┘

  The Preset Model

  This is the part you're rightly excited about. Each station directory is a preset — a complete personality:

  # stations/home-chill/station.yaml
  station_name: "Late Night Ambient"
  plugins:
    presenter:
      system_prompt: "You are a late-night ambient DJ. Whisper-soft,
                      poetic, contemplative..."
      periodic_interval: 600
    simple_playlist_feeder:
      no_repeat_count: 20

  # stations/work-beats/station.yaml
  station_name: "Office Groove"
  plugins:
    presenter:
      system_prompt: "You are an upbeat office DJ. Keep energy up,
                      short quips between tracks..."
      periodic_interval: 300

  New station = copy a directory, change the persona. The code never changes — just the personality file.

  What We'd Need to Build

  1. Move config into stations/ directory structure
  2. Modify run_radiodan.sh to accept STATION=xxx env var, read from stations/$STATION/
  3. Modify bridge/config.py to look in the station directory
  4. Set up private git on one of your servers (Forgejo is great — single binary, minimal resources)
  5. Install Tailscale on both servers + devices
