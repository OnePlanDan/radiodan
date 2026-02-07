#!/usr/bin/env bash
#
# RadioDan Control Script
# Usage: ./run_radiodan.sh [command]
#
# Commands:
#   status   - Show what's running (default)
#   start    - Start all services
#   stop     - Stop all services
#   restart          - Restart all services
#   restart-pyhost   - Restart only the Python bridge (after code changes)
#   restart-docker   - Restart only Icecast + Liquidsoap containers
#   logs             - Tail logs from all services
#   bot              - Start only the Telegram bot (foreground)
#   audio    - Start only audio infrastructure
#   url      - Show stream URL
#

set -e
cd "$(dirname "$0")"

# Ensure uv is in PATH
export PATH="$HOME/.local/bin:$PATH"

# ═══════════════════════════════════════════════════════════════════════════════
# Station resolution
# ═══════════════════════════════════════════════════════════════════════════════

# Priority: STATION env var > .station file > legacy config/ fallback
resolve_station() {
    if [[ -n "${STATION:-}" ]]; then
        echo "$STATION"
        return
    fi

    if [[ -f .station ]]; then
        local s
        s=$(tr -d '[:space:]' < .station)
        if [[ -n "$s" ]]; then
            echo "$s"
            return
        fi
    fi

    # Legacy: if config/radiodan.yaml exists and no stations/ dir, run in legacy mode
    if [[ -f config/radiodan.yaml ]] && [[ ! -d stations ]]; then
        echo "__legacy__"
        return
    fi

    echo ""
}

# ═══════════════════════════════════════════════════════════════════════════════
# Colors and formatting
# ═══════════════════════════════════════════════════════════════════════════════
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m' # No Color

# Status indicators
OK="${GREEN}●${NC}"
FAIL="${RED}●${NC}"
WARN="${YELLOW}●${NC}"
OFF="${DIM}○${NC}"

# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════
info()  { echo -e "${CYAN}►${NC} $*"; }
gold()  { echo -e "${YELLOW}★${NC} ${DIM}$*${NC}"; }
error() { echo -e "${RED}✗${NC} $*" >&2; }

header() {
    local stn="${STATION_NAME:-RadioDan}"
    echo
    echo -e "${BOLD}═══════════════════════════════════════════════════════════${NC}"
    echo -e "${BOLD}  🎧 ${stn} ${DIM}— $1${NC}"
    echo -e "${BOLD}═══════════════════════════════════════════════════════════${NC}"
}

get_local_ip() {
    # Get LAN IP for stream URL
    ip route get 1 2>/dev/null | awk '{print $7; exit}' || hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost"
}

# ═══════════════════════════════════════════════════════════════════════════════
# Station environment
# ═══════════════════════════════════════════════════════════════════════════════

STATION_RESOLVED=$(resolve_station)

if [[ -z "$STATION_RESOLVED" ]]; then
    echo
    error "No station specified. Set STATION env var or create a .station file."
    echo
    echo -e "  ${DIM}Available stations:${NC}"
    if [[ -d stations ]]; then
        for d in stations/*/; do
            [[ -f "$d/station.yaml" ]] && echo -e "    ${CYAN}$(basename "$d")${NC}"
        done
    else
        echo -e "    ${DIM}(no stations/ directory found)${NC}"
    fi
    echo
    echo -e "  ${DIM}Usage:${NC}"
    echo -e "    ${CYAN}STATION=radio-dan ./run_radiodan.sh start${NC}"
    echo -e "    ${DIM}or${NC}"
    echo -e "    ${CYAN}echo radio-dan > .station${NC}"
    exit 1
fi

if [[ "$STATION_RESOLVED" == "__legacy__" ]]; then
    # Legacy mode: use config/ directory directly
    export RADIODAN_STATION_DIR="$(pwd)/config"
    if [[ -f .env ]]; then
        set -a; source .env; set +a
    fi
    LOG_FILE="$LOG_FILE"
else
    STATION_DIR="stations/$STATION_RESOLVED"

    if [[ ! -f "$STATION_DIR/station.yaml" ]]; then
        error "Station '$STATION_RESOLVED' not found (no $STATION_DIR/station.yaml)"
        exit 1
    fi

    export RADIODAN_STATION_DIR="$(pwd)/$STATION_DIR"
    export STATION="$STATION_RESOLVED"

    if [[ -f "$STATION_DIR/.env" ]]; then
        set -a; source "$STATION_DIR/.env"; set +a
    else
        error "No .env file for station '$STATION_RESOLVED'"
        error "Copy $STATION_DIR/.env.example to $STATION_DIR/.env and configure it."
        exit 1
    fi
    LOG_FILE="/tmp/radiodan-${STATION_RESOLVED}.log"
fi

# ═══════════════════════════════════════════════════════════════════════════════
# Status checks
# ═══════════════════════════════════════════════════════════════════════════════
check_docker() {
    docker info &>/dev/null
}

check_icecast() {
    docker compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -q "icecast"
}

check_liquidsoap() {
    docker compose ps --status running --format '{{.Service}}' 2>/dev/null | grep -q "liquidsoap"
}

check_bot() {
    pgrep -f "[b]ridge.main" &>/dev/null
}

check_stream() {
    curl -s --max-time 2 -o /dev/null -w "%{http_code}" "http://localhost:49994/stream" 2>/dev/null | grep -q "200"
}

check_env() {
    # Station .env was already sourced — just check the token is set and not placeholder
    [[ -n "${TELEGRAM_BOT_TOKEN:-}" ]] && [[ "$TELEGRAM_BOT_TOKEN" != "your_bot_token_here" ]]
}

music_count() {
    find music -type f \( -name "*.mp3" -o -name "*.ogg" -o -name "*.wav" -o -name "*.flac" \) 2>/dev/null | wc -l | tr -d ' '
}

# ═══════════════════════════════════════════════════════════════════════════════
# Commands
# ═══════════════════════════════════════════════════════════════════════════════
cmd_status() {
    header "Status"

    local ip=$(get_local_ip)
    local tracks=$(music_count)

    echo
    echo -e "  ${BOLD}Infrastructure${NC}"

    if check_docker; then
        echo -e "    $OK Docker         ${DIM}daemon running${NC}"
    else
        echo -e "    $FAIL Docker         ${RED}not running${NC}"
        gold "Start Docker Desktop or 'sudo systemctl start docker'"
    fi

    if check_icecast; then
        echo -e "    $OK Icecast        ${DIM}streaming on :49994${NC}"
    else
        echo -e "    $OFF Icecast        ${DIM}stopped${NC}"
    fi

    if check_liquidsoap; then
        echo -e "    $OK Liquidsoap     ${DIM}mixing audio${NC}"
    else
        echo -e "    $OFF Liquidsoap     ${DIM}stopped${NC}"
    fi

    if check_stream; then
        echo -e "    $OK Stream         ${GREEN}live!${NC}"
    else
        echo -e "    $OFF Stream         ${DIM}no audio${NC}"
    fi

    echo
    echo -e "  ${BOLD}Bot & Config${NC}"

    if check_env; then
        echo -e "    $OK .env           ${DIM}configured${NC}"
    else
        echo -e "    $FAIL .env           ${RED}missing or empty${NC}"
        gold "Copy .env.example to .env and add your Telegram token"
    fi

    if check_bot; then
        echo -e "    $OK Telegram Bot   ${DIM}running${NC}"
    else
        echo -e "    $OFF Telegram Bot   ${DIM}stopped${NC}"
    fi

    echo
    echo -e "  ${BOLD}Media${NC}"
    if [[ $tracks -gt 0 ]]; then
        echo -e "    $OK Music          ${DIM}$tracks tracks loaded${NC}"
    else
        echo -e "    $WARN Music          ${YELLOW}no tracks found${NC}"
        gold "Add .mp3/.ogg files to ./music/ for background music"
    fi

    echo
    echo -e "  ${BOLD}Stream URL${NC}"
    echo -e "    ${CYAN}http://${ip}:49994/stream${NC}"
    gold "Open in VLC, browser, or any audio player"

    echo
}

cmd_start() {
    header "Starting"

    if ! check_docker; then
        error "Docker is not running!"
        exit 1
    fi

    if ! check_env; then
        error ".env not configured! Copy .env.example and add your Telegram token."
        exit 1
    fi

    # Start audio infrastructure
    info "Starting Icecast + Liquidsoap..."
    docker compose up -d
    gold "Audio containers starting (may take a few seconds)"

    # Wait for Icecast to be healthy
    echo -n "    Waiting for Icecast"
    for i in {1..15}; do
        if check_icecast; then
            echo -e " ${GREEN}ready${NC}"
            break
        fi
        echo -n "."
        sleep 1
    done

    # Start bot in background
    info "Starting Telegram bot..."
    if check_bot; then
        echo -e "    ${DIM}(already running)${NC}"
    else
        # Use uv to run the bot
        nohup uv run python -m bridge.main > $LOG_FILE 2>&1 &
        sleep 3
        if check_bot; then
            # Verify the bot actually initialized (not just process alive)
            if grep -q "is running!" $LOG_FILE 2>/dev/null; then
                echo -e "    ${GREEN}Bot started and healthy${NC} (PID: $(pgrep -f 'bridge.main' | head -1))"
            else
                echo -e "    ${GREEN}Bot started${NC} (PID: $(pgrep -f 'bridge.main' | head -1))"
                gold "Still initializing — check logs if plugins don't respond"
            fi
            gold "Logs at $LOG_FILE"
        else
            error "Bot failed to start. Check $LOG_FILE"
        fi
    fi

    echo
    cmd_url
}

cmd_stop() {
    header "Stopping"

    info "Stopping Telegram bot..."
    # Use [b] trick to avoid pkill matching its own shell process
    pkill -f "[b]ridge.main" 2>/dev/null || echo -e "    ${DIM}(not running)${NC}"
    # Wait for process to gracefully exit (up to 10s)
    for i in {1..20}; do
        pgrep -f "[b]ridge.main" >/dev/null 2>&1 || break
        sleep 0.5
    done
    # SIGKILL fallback if still alive
    if pgrep -f "[b]ridge.main" >/dev/null 2>&1; then
        echo -e "    ${YELLOW}Graceful shutdown timed out, forcing...${NC}"
        pkill -9 -f "[b]ridge.main" 2>/dev/null
        sleep 1
    fi

    info "Stopping audio containers..."
    docker compose down 2>/dev/null || echo -e "    ${DIM}(not running)${NC}"

    echo
    gold "All services stopped"
    echo
}

cmd_restart() {
    cmd_stop
    sleep 1
    cmd_start
}

cmd_restart_pyhost() {
    header "Restarting Python Bridge"

    info "Stopping Python bridge..."
    pkill -f "[b]ridge.main" 2>/dev/null || echo -e "    ${DIM}(not running)${NC}"
    for i in {1..20}; do
        pgrep -f "[b]ridge.main" >/dev/null 2>&1 || break
        sleep 0.5
    done
    # SIGKILL fallback if still alive
    if pgrep -f "[b]ridge.main" >/dev/null 2>&1; then
        echo -e "    ${YELLOW}Graceful shutdown timed out, forcing...${NC}"
        pkill -9 -f "[b]ridge.main" 2>/dev/null
        sleep 1
    fi

    info "Starting Python bridge..."
    nohup uv run python -m bridge.main >> $LOG_FILE 2>&1 &
    sleep 2

    if check_bot; then
        echo -e "    ${GREEN}Bridge restarted${NC} (PID: $(pgrep -f '[b]ridge.main' | head -1))"
    else
        error "Bridge failed to start. Last 20 lines:"
        tail -20 $LOG_FILE 2>/dev/null
    fi

    echo
    info "Recent log output:"
    tail -15 $LOG_FILE 2>/dev/null
    echo
}

cmd_restart_docker() {
    header "Restarting Docker Containers"

    info "Restarting Icecast + Liquidsoap..."
    docker compose restart 2>/dev/null

    echo -n "    Waiting for Icecast"
    for i in {1..15}; do
        if check_icecast; then
            echo -e " ${GREEN}ready${NC}"
            break
        fi
        echo -n "."
        sleep 1
    done

    if check_liquidsoap; then
        echo -e "    $OK Liquidsoap     ${DIM}running${NC}"
    else
        echo -e "    $FAIL Liquidsoap     ${RED}not responding${NC}"
    fi

    echo
}

cmd_logs() {
    header "Logs"
    echo
    info "Tailing all logs (Ctrl+C to exit)..."
    gold "Bot logs from $LOG_FILE, Docker logs from containers"
    echo

    # Tail both docker and bot logs
    docker compose logs -f --tail=20 &
    DOCKER_PID=$!

    tail -f $LOG_FILE 2>/dev/null &
    TAIL_PID=$!

    trap "kill $DOCKER_PID $TAIL_PID 2>/dev/null" EXIT
    wait
}

cmd_bot() {
    header "Telegram Bot"

    if ! check_env; then
        error ".env not configured!"
        exit 1
    fi

    info "Starting bot in foreground (Ctrl+C to stop)..."
    gold "This runs the bot directly - useful for debugging"
    echo

    uv run python -m bridge.main
}

cmd_audio() {
    header "Audio Infrastructure"

    info "Starting Icecast + Liquidsoap..."
    docker compose up -d

    echo
    gold "Audio containers started"
    gold "Stream will be at http://$(get_local_ip):49994/stream"
    echo
}

cmd_url() {
    local ip=$(get_local_ip)
    echo
    echo -e "  ${BOLD}🎵 Stream URL:${NC}"
    echo -e "     ${CYAN}http://${ip}:49994/stream${NC}"
    echo
    gold "Works in VLC, browser, or mobile audio apps"
    echo
}

cmd_stations() {
    echo
    echo -e "  ${BOLD}Available Stations${NC}"
    echo
    if [[ -d stations ]]; then
        for d in stations/*/; do
            local name
            name=$(basename "$d")
            if [[ -f "$d/station.yaml" ]]; then
                local sn
                sn=$(grep '^station_name:' "$d/station.yaml" | sed 's/station_name: *//')
                local has_env=""
                if [[ -f "$d/.env" ]]; then
                    has_env="${GREEN}(configured)${NC}"
                else
                    has_env="${YELLOW}(needs .env)${NC}"
                fi
                local active=""
                [[ "${STATION_RESOLVED:-}" == "$name" ]] && active=" ${CYAN}<- active${NC}"
                echo -e "    ${BOLD}$name${NC} — $sn $has_env$active"
            fi
        done
    else
        echo -e "    ${DIM}No stations/ directory found${NC}"
    fi
    echo
}

cmd_help() {
    cat << 'EOF'

  Usage: ./run_radiodan.sh [command]

  Commands:
    status    Show what's running (default)
    start     Start all services (audio + bot)
    stop      Stop all services
    restart          Restart everything
    restart-pyhost   Restart only the Python bridge
    restart-docker   Restart only Icecast + Liquidsoap
    logs             Tail logs from all services
    bot              Run Telegram bot in foreground
    audio     Start only audio (Icecast + Liquidsoap)
    url       Show the stream URL
    stations  List available station presets
    help      Show this help

  Station selection:
    STATION=radio-dan ./run_radiodan.sh start   # Env var
    echo radio-dan > .station                    # Persistent default

  Examples:
    ./run_radiodan.sh              # Check status
    ./run_radiodan.sh start        # Start everything
    ./run_radiodan.sh stations     # List presets
    ./run_radiodan.sh bot          # Debug the bot

EOF
}

# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════
case "${1:-status}" in
    status)  cmd_status ;;
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart)        cmd_restart ;;
    restart-pyhost) cmd_restart_pyhost ;;
    restart-docker) cmd_restart_docker ;;
    logs)           cmd_logs ;;
    bot)         cmd_bot ;;
    audio)   cmd_audio ;;
    url)     cmd_url ;;
    stations) cmd_stations ;;
    help|-h|--help) cmd_help ;;
    *)
        error "Unknown command: $1"
        cmd_help
        exit 1
        ;;
esac
