#!/bin/bash
# MP3 Health Scanner — finds corrupt/silent files in the music library
# Creates a symlink folder for easy Samba browsing of damaged files
#
# Usage: ./scan_mp3_health.sh [music_dir]

MUSIC_DIR="$(realpath "${1:-./music}")"
DAMAGED_DIR="${MUSIC_DIR}/_damaged"
REPORT_FILE="${DAMAGED_DIR}/report.txt"
SILENCE_THRESHOLD=30    # seconds of consecutive silence to flag
NOISE_FLOOR="-40dB"     # what counts as "silence"
ERROR_THRESHOLD=10      # need this many decode errors to flag (filters old-rip jank)

mkdir -p "$DAMAGED_DIR"

# Clear previous scan
rm -f "$DAMAGED_DIR"/*.mp3 "$REPORT_FILE"

echo "MP3 Health Scan — $(date)" > "$REPORT_FILE"
echo "Music dir: $MUSIC_DIR" >> "$REPORT_FILE"
echo "Flagging: silence > ${SILENCE_THRESHOLD}s or >= ${ERROR_THRESHOLD} decode errors" >> "$REPORT_FILE"
echo "========================================" >> "$REPORT_FILE"
echo "" >> "$REPORT_FILE"

total=$(find "$MUSIC_DIR" -name "*.mp3" -not -path "*/_damaged/*" | wc -l)
echo "Scanning $total MP3 files..."
echo "Total files to scan: $total" >> "$REPORT_FILE"
echo "" >> "$REPORT_FILE"

count=0
flagged=0

find "$MUSIC_DIR" -name "*.mp3" -not -path "*/_damaged/*" -print0 | while IFS= read -r -d '' file; do
    count=$((count + 1))

    # Progress every 100 files
    if [ $((count % 100)) -eq 0 ]; then
        echo "[$count/$total] scanning... ($flagged flagged so far)"
    fi

    # Run silencedetect + capture decode errors
    result=$(ffmpeg -v error -i "$file" -af "silencedetect=noise=${NOISE_FLOOR}:d=${SILENCE_THRESHOLD}" -f null /dev/null 2>&1)

    issues=""
    reason=""

    # Count decode errors (not just presence — need a threshold)
    error_count=$(echo "$result" | grep -ciE "invalid|error|corrupt|missing|damaged")
    if [ "$error_count" -ge "$ERROR_THRESHOLD" ]; then
        sample=$(echo "$result" | grep -iE "invalid|error|corrupt|missing|damaged" | sort | uniq -c | sort -rn | head -3)
        issues="${issues}DECODE_ERRORS (${error_count}x):\n${sample}\n"
        reason="corrupt"
    fi

    # Check for long silence
    has_silence=false
    if echo "$result" | grep -q "silence_duration"; then
        while IFS= read -r line; do
            duration=$(echo "$line" | grep -oP 'silence_duration: \K[0-9.]+')
            if [ -n "$duration" ]; then
                dur_int=${duration%.*}
                if [ "$dur_int" -ge "$SILENCE_THRESHOLD" ] 2>/dev/null; then
                    issues="${issues}SILENCE: ${duration}s silent section\n"
                    has_silence=true
                    reason="silent"
                fi
            fi
        done <<< "$(echo "$result" | grep "silence_end")"
    fi

    # Flag files where silence starts but never ends (silence till EOF)
    if echo "$result" | grep -q "silence_start" && ! echo "$result" | grep -q "silence_end"; then
        start_time=$(echo "$result" | grep -oP 'silence_start: \K[0-9.]+' | tail -1)
        if [ -n "$start_time" ]; then
            issues="${issues}SILENCE_TO_EOF: silence from ${start_time}s to end of file\n"
            has_silence=true
            reason="silent"
        fi
    fi

    if [ -n "$issues" ]; then
        flagged=$((flagged + 1))
        rel_path="${file#$MUSIC_DIR/}"

        echo "--- [$flagged] $rel_path ---" >> "$REPORT_FILE"
        echo -e "$issues" >> "$REPORT_FILE"

        # Create symlink — flatten path for browsability.
        # `-r` makes the symlink target relative to the symlink's location, so
        # the link still resolves correctly inside the Liquidsoap Docker container
        # (which sees `/music/_damaged/...` but not the host's absolute path).
        link_name=$(echo "$rel_path" | sed 's|/| - |g')
        ln -srf "$file" "$DAMAGED_DIR/$link_name"

        echo "FLAGGED [$reason]: $rel_path"
    fi
done

echo "" >> "$REPORT_FILE"
echo "========================================" >> "$REPORT_FILE"
echo "Scan complete: $count files scanned, $flagged flagged" >> "$REPORT_FILE"
echo "Symlinks in: $DAMAGED_DIR/" >> "$REPORT_FILE"

echo ""
echo "Done! $count scanned, $flagged damaged files found."
echo "Report: $REPORT_FILE"
echo "Browse damaged: $DAMAGED_DIR/"
