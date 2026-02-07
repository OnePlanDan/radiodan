/**
 * RadioDan — DAW-Like Timeline Visualization
 *
 * Connects to the SSE endpoint, renders a multi-lane scrolling timeline
 * with a playhead, auto-scroll, and hover tooltips.
 *
 * The timeline shows audible events (music + voice lanes) and API calls
 * (LLM/TTS) in an "api" lane. System events also appear in the Activity
 * table below the timeline.
 */

// ============================================================
// Configuration
// ============================================================

const PX_PER_SECOND = 1;           // Zoom level: 1px per second — 5-min song ≈ 300px
const HISTORY_SECONDS = 300;       // 5 minutes of past visible
const FUTURE_SECONDS = 3600;       // 60 minutes of future visible (shows upcoming tracks)
const LANE_HEIGHT = 40;            // Height of each lane in pixels
const AXIS_HEIGHT = 28;            // Height of the time axis
const LANE_LABEL_WIDTH = 110;      // Width of the sticky lane labels
const MAX_ACTIVITY_ROWS = 20;      // Max rows in the activity table

const LANE_COLORS = {
    music:  '#6c5ce7',
    time:   '#f39c12',
    api:    '#e67e22',
    _palette: ['#3498db', '#2ecc71', '#e74c3c', '#1abc9c', '#e67e22'],
};

const STATUS_OPACITY = {
    active: 1.0,
    scheduled: 0.5,
    completed: 0.8,
    failed: 0.4,
    cancelled: 0.3,
};

// ============================================================
// State
// ============================================================

class TimelineState {
    constructor() {
        this.events = new Map();         // event_id -> event object (audible only)
        this.systemEvents = new Map();   // event_id -> event object (system lane)
        this.upcomingTracks = [];        // upcoming queue from server
        this.laneOrder = [];             // ordered lane IDs
        this.laneColors = new Map();     // lane_id -> color
        this.serverTimeOffset = 0;       // server_time - local_time
        this.playbackElapsed = 0;
        this.playbackRemaining = 0;
        this.currentTrackEndAt = 0;      // absolute timestamp when current track ends
        this.crossfadeDuration = 5.0;
        this._paletteIndex = 0;
    }

    getLaneColor(laneId) {
        if (this.laneColors.has(laneId)) return this.laneColors.get(laneId);
        let color;
        if (LANE_COLORS[laneId]) {
            color = LANE_COLORS[laneId];
        } else {
            const palette = LANE_COLORS._palette;
            color = palette[this._paletteIndex % palette.length];
            this._paletteIndex++;
        }
        this.laneColors.set(laneId, color);
        return color;
    }

    ensureLane(laneId) {
        if (!this.laneOrder.includes(laneId)) {
            // music always first, api always last, voice lanes in the middle
            if (laneId === 'music') {
                this.laneOrder.unshift(laneId);
            } else if (laneId === 'api') {
                this.laneOrder.push(laneId);
            } else {
                // Insert before 'api' if it exists, otherwise append
                const apiIdx = this.laneOrder.indexOf('api');
                if (apiIdx !== -1) {
                    this.laneOrder.splice(apiIdx, 0, laneId);
                } else {
                    this.laneOrder.push(laneId);
                }
            }
            this.getLaneColor(laneId);
        }
    }

    applySnapshot(events) {
        this.events.clear();
        this.systemEvents.clear();
        for (const ev of events) {
            if (ev.lane === 'system') {
                // Keep in systemEvents for the Activity table
                this.systemEvents.set(ev.id, ev);
                // Also render on the timeline in the 'api' lane
                ev.lane = 'api';
                this.events.set(ev.id, ev);
                this.ensureLane('api');
            } else {
                this.events.set(ev.id, ev);
                this.ensureLane(ev.lane);
            }
        }
    }

    applyUpdate(msg) {
        const { action, event } = msg;
        if (!event) return;

        if (action === 'start') {
            // Full event object — route by lane
            if (event.lane === 'system') {
                // Keep in systemEvents for the Activity table
                this.systemEvents.set(event.id, event);
                renderActivityTable();
                // Also render on the timeline in the 'api' lane
                event.lane = 'api';
                this.events.set(event.id, event);
                this.ensureLane('api');
            } else {
                this.events.set(event.id, event);
                this.ensureLane(event.lane);
            }
            return;
        }

        // end/update are partial (no lane) — look up by ID in both maps
        const systemExisting = this.systemEvents.get(event.id);
        if (systemExisting) {
            Object.assign(systemExisting, event);
            renderActivityTable();
            // Also exists in events map — update there too (same object ref)
        }

        const existing = this.events.get(event.id);
        if (existing) {
            // If it's a system event, the object is shared so already updated above,
            // but for non-system events we still need to merge
            if (!systemExisting) {
                Object.assign(existing, event);
            }
        }
    }
}

const state = new TimelineState();

// ============================================================
// Time helpers
// ============================================================

function serverNow() {
    return Date.now() / 1000 + state.serverTimeOffset;
}

function formatTime(ts) {
    const d = new Date(ts * 1000);
    const h = d.getHours().toString().padStart(2, '0');
    const m = d.getMinutes().toString().padStart(2, '0');
    const s = d.getSeconds().toString().padStart(2, '0');
    return `${h}:${m}:${s}`;
}

function formatTimeShort(ts) {
    const d = new Date(ts * 1000);
    const h = d.getHours().toString().padStart(2, '0');
    const m = d.getMinutes().toString().padStart(2, '0');
    return `${h}:${m}`;
}

function formatDuration(seconds) {
    if (!seconds || seconds <= 0) return '\u2014';
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    return `${m}:${s.toString().padStart(2, '0')}`;
}

// ============================================================
// SSE Connection
// ============================================================

let eventSource = null;
let reconnectDelay = 1000;

function connectSSE() {
    eventSource = new EventSource('/api/timeline/events');

    eventSource.addEventListener('snapshot', (e) => {
        const events = JSON.parse(e.data);
        state.applySnapshot(events);
        rebuildLanes();
        renderActivityTable();
        reconnectDelay = 1000;
    });

    eventSource.addEventListener('playback_state', (e) => {
        const data = JSON.parse(e.data);
        state.serverTimeOffset = data.server_time - Date.now() / 1000;
        state.playbackElapsed = data.elapsed;
        state.playbackRemaining = data.remaining;
        // Anchor: absolute timestamp for when the current track ends (drift-free)
        state.currentTrackEndAt = data.server_time + data.remaining;
        if (data.crossfade_duration !== undefined) {
            state.crossfadeDuration = data.crossfade_duration;
        }
    });

    eventSource.addEventListener('upcoming', (e) => {
        state.upcomingTracks = JSON.parse(e.data);
        rebuildUpcoming();
    });

    eventSource.addEventListener('event_update', (e) => {
        const msg = JSON.parse(e.data);
        state.applyUpdate(msg);
        // Incremental DOM update for all events on the timeline
        if (!msg.event) return;
        const ev = state.events.get(msg.event.id);
        if (ev) {
            if (msg.action === 'start') {
                addEventElement(ev);
            } else {
                updateEventElement(msg.event);
            }
        }
    });

    eventSource.onerror = () => {
        eventSource.close();
        setTimeout(connectSSE, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 2, 30000);
    };
}

// ============================================================
// DOM References
// ============================================================

const container = document.getElementById('timeline');
const axisEl = document.getElementById('tlAxis');
const lanesEl = document.getElementById('tlLanes');
const playheadEl = document.getElementById('tlPlayhead');
const snapBtn = document.getElementById('snapLive');
const activityBody = document.getElementById('activityBody');

// ============================================================
// Lane DOM Management
// ============================================================

const laneElements = new Map();   // lane_id -> { row, eventsArea }
const eventElements = new Map();  // event_id -> DOM element
const upcomingElements = [];      // DOM elements for upcoming tracks

function rebuildLanes() {
    // Remove old lane rows that no longer exist
    for (const [id, els] of laneElements) {
        if (!state.laneOrder.includes(id)) {
            els.row.remove();
            laneElements.delete(id);
        }
    }
    // Add/reorder lanes
    for (let i = 0; i < state.laneOrder.length; i++) {
        const laneId = state.laneOrder[i];
        let els = laneElements.get(laneId);
        if (!els) {
            els = createLaneRow(laneId);
            laneElements.set(laneId, els);
        }
        // Ensure correct DOM order
        if (lanesEl.children[i] !== els.row) {
            lanesEl.insertBefore(els.row, lanesEl.children[i] || null);
        }
    }
    // Rebuild all event elements from state
    eventElements.clear();
    for (const els of laneElements.values()) {
        els.eventsArea.innerHTML = '';
    }
    // Sort events by start time so stagger is chronologically consistent
    const sortedEvents = Array.from(state.events.values())
        .sort((a, b) => a.started_at - b.started_at);
    for (const ev of sortedEvents) {
        addEventElement(ev);
    }
    // Rebuild upcoming track elements
    rebuildUpcoming();
}

function createLaneRow(laneId) {
    const row = document.createElement('div');
    row.className = 'tl-lane';
    row.style.height = LANE_HEIGHT + 'px';

    const label = document.createElement('div');
    label.className = 'tl-lane-label';
    label.textContent = laneId;
    label.style.width = LANE_LABEL_WIDTH + 'px';
    label.style.color = state.getLaneColor(laneId);

    const eventsArea = document.createElement('div');
    eventsArea.className = 'tl-lane-events';

    row.appendChild(label);
    row.appendChild(eventsArea);
    return { row, eventsArea };
}

// ============================================================
// Event Element Management
// ============================================================

function getMusicEventCount() {
    // Count real music events (not upcoming) for deterministic stagger
    let count = 0;
    for (const ev of state.events.values()) {
        if (ev.lane === 'music') count++;
    }
    return count;
}

function getLaneStaggerIndex(ev) {
    // Chronological rank within lane: how many same-lane events started before this one?
    // Stable regardless of insertion order or rebuild vs live-add.
    let idx = 0;
    for (const other of state.events.values()) {
        if (other.lane === ev.lane && other.started_at < ev.started_at) idx++;
    }
    return idx;
}

// Keep backward-compatible alias used by music lane
function getMusicStaggerIndex(ev) {
    return getLaneStaggerIndex(ev);
}

function addEventElement(ev) {
    const els = laneElements.get(ev.lane);
    if (!els) return;

    const el = document.createElement('div');
    el.className = 'tl-event';
    el.dataset.eventId = ev.id;
    el.title = ev.title + (ev.details?.text ? '\n' + ev.details.text : '');

    const color = state.getLaneColor(ev.lane);
    el.style.background = color;

    // Music & API lanes: half-height with brick-wall stagger
    if (ev.lane === 'music' || ev.lane === 'api') {
        // Chronological rank — stable across rebuild and live-add
        const idx = getLaneStaggerIndex(ev);
        el.style.height = '16px';
        el.style.top = (idx % 2 === 0 ? 2 : 22) + 'px';
    } else {
        el.style.height = (LANE_HEIGHT - 8) + 'px';
        el.style.top = '4px';
    }

    applyEventStatus(el, ev.status);

    // Inner label
    const inner = document.createElement('span');
    inner.className = 'tl-event-label';
    inner.textContent = ev.title;
    el.appendChild(inner);

    els.eventsArea.appendChild(el);
    eventElements.set(ev.id, el);
}

function updateEventElement(ev) {
    const el = eventElements.get(ev.id);
    if (!el) return;
    if (ev.status) applyEventStatus(el, ev.status);
    if (ev.title) {
        const label = el.querySelector('.tl-event-label');
        if (label) label.textContent = ev.title;
        el.title = ev.title;
    }
}

function applyEventStatus(el, status) {
    el.classList.remove('active', 'scheduled', 'completed', 'failed', 'cancelled');
    if (status) {
        el.classList.add(status);
        el.style.opacity = STATUS_OPACITY[status] ?? 1.0;
    }
}

// ============================================================
// Upcoming Tracks (projected on music lane)
// ============================================================

function rebuildUpcoming() {
    // Remove old upcoming elements
    for (const el of upcomingElements) {
        el.remove();
    }
    upcomingElements.length = 0;

    const musicLane = laneElements.get('music');
    if (!musicLane) return;

    const color = state.getLaneColor('music');

    for (let i = 0; i < state.upcomingTracks.length; i++) {
        const track = state.upcomingTracks[i];
        const el = document.createElement('div');
        el.className = 'tl-event upcoming-track';
        el.style.background = color;

        // Continue brick-wall stagger from real music events
        const idx = getMusicEventCount() + i;
        el.style.height = '16px';
        el.style.top = (idx % 2 === 0 ? 2 : 22) + 'px';

        const label = track.artist
            ? `${track.artist} \u2014 ${track.title}`
            : track.title || 'Unknown';
        el.title = label + ` (${formatDuration(track.duration_seconds)})`;

        const inner = document.createElement('span');
        inner.className = 'tl-event-label';
        inner.textContent = label;
        el.appendChild(inner);

        musicLane.eventsArea.appendChild(el);
        upcomingElements.push(el);
    }
}

// ============================================================
// Auto-scroll + Drag
// ============================================================

let isLive = true;
let manualOffset = 0;  // seconds offset from live (when dragging)
let isDragging = false;
let dragStartX = 0;
let dragStartOffset = 0;

container.addEventListener('mousedown', (e) => {
    if (e.target.closest('.tl-lane-label') || e.target.closest('.tl-snap-live')) return;
    isDragging = true;
    dragStartX = e.clientX;
    dragStartOffset = manualOffset;
    container.style.cursor = 'grabbing';
});

window.addEventListener('mousemove', (e) => {
    if (!isDragging) return;
    const dx = e.clientX - dragStartX;
    manualOffset = dragStartOffset + dx / PX_PER_SECOND;
    isLive = false;
    snapBtn.classList.remove('hidden');
});

window.addEventListener('mouseup', () => {
    if (isDragging) {
        isDragging = false;
        container.style.cursor = '';
    }
});

snapBtn.addEventListener('click', () => {
    isLive = true;
    manualOffset = 0;
    snapBtn.classList.add('hidden');
});

// ============================================================
// Render Loop
// ============================================================

function render() {
    const now = serverNow();
    const viewCenter = isLive ? now : now + manualOffset;
    const viewStart = viewCenter - HISTORY_SECONDS;
    const viewEnd = viewCenter + FUTURE_SECONDS;
    const totalSeconds = HISTORY_SECONDS + FUTURE_SECONDS;
    const totalWidth = totalSeconds * PX_PER_SECOND;

    // Container width
    const containerWidth = container.clientWidth - LANE_LABEL_WIDTH;

    // Position all audible event elements
    for (const ev of state.events.values()) {
        const el = eventElements.get(ev.id);
        if (!el) continue;

        const evStart = ev.started_at;
        const evDuration = ev.details?.duration_seconds;
        const evEnd = ev.ended_at
            || (evDuration ? evStart + evDuration : (ev.status === 'active' ? now : evStart + 2));

        // Skip if completely outside the view
        if (evEnd < viewStart || evStart > viewEnd) {
            el.style.display = 'none';
            continue;
        }

        el.style.display = '';

        // Calculate pixel positions relative to the events area
        const leftPx = (evStart - viewStart) * PX_PER_SECOND;
        const widthPx = Math.max((evEnd - evStart) * PX_PER_SECOND, 3);  // min 3px visibility

        el.style.left = leftPx + 'px';
        el.style.width = widthPx + 'px';
    }

    // Position upcoming track elements — use absolute anchor to prevent drift
    const cf = state.crossfadeDuration;
    let nextStart = state.currentTrackEndAt - cf;
    for (let i = 0; i < upcomingElements.length; i++) {
        const el = upcomingElements[i];
        const track = state.upcomingTracks[i];
        if (!track) { el.style.display = 'none'; continue; }

        const dur = track.duration_seconds || 180;  // fallback 3 min if unknown
        const evStart = nextStart;
        const evEnd = evStart + dur;

        // Advance start for the next track (accounting for crossfade overlap)
        nextStart = evEnd - cf;

        if (evEnd < viewStart || evStart > viewEnd) {
            el.style.display = 'none';
            continue;
        }

        el.style.display = '';
        const leftPx = (evStart - viewStart) * PX_PER_SECOND;
        const widthPx = Math.max((evEnd - evStart) * PX_PER_SECOND, 3);
        el.style.left = leftPx + 'px';
        el.style.width = widthPx + 'px';
    }

    // Set events area width
    for (const els of laneElements.values()) {
        els.eventsArea.style.width = totalWidth + 'px';
    }

    // Playhead: positioned at the "now" line
    const playheadPx = LANE_LABEL_WIDTH + (now - viewStart) * PX_PER_SECOND;
    playheadEl.style.left = playheadPx + 'px';
    playheadEl.style.display = (playheadPx >= LANE_LABEL_WIDTH && playheadPx <= container.clientWidth) ? '' : 'none';

    // Time axis ticks
    renderAxis(viewStart, viewEnd, containerWidth);

    requestAnimationFrame(render);
}

// ============================================================
// Time Axis
// ============================================================

function renderAxis(viewStart, viewEnd, containerWidth) {
    // Determine tick interval based on zoom
    const totalSeconds = viewEnd - viewStart;
    let tickInterval;
    if (totalSeconds < 120) tickInterval = 10;
    else if (totalSeconds < 300) tickInterval = 30;
    else if (totalSeconds < 900) tickInterval = 60;
    else tickInterval = 300;

    // Reuse or create tick elements
    const firstTick = Math.ceil(viewStart / tickInterval) * tickInterval;
    const ticks = [];
    for (let t = firstTick; t <= viewEnd; t += tickInterval) {
        ticks.push(t);
    }

    // Clear and rebuild (simple approach — axis ticks are lightweight)
    axisEl.innerHTML = '';
    axisEl.style.paddingLeft = LANE_LABEL_WIDTH + 'px';

    for (const t of ticks) {
        const tick = document.createElement('div');
        tick.className = 'tl-tick';
        const leftPx = (t - viewStart) * PX_PER_SECOND;
        tick.style.left = (LANE_LABEL_WIDTH + leftPx) + 'px';

        const isMajor = t % 60 === 0;
        tick.textContent = isMajor ? formatTimeShort(t) : ':' + new Date(t * 1000).getSeconds().toString().padStart(2, '0');
        if (isMajor) tick.classList.add('major');

        axisEl.appendChild(tick);
    }
}

// ============================================================
// Activity Table (system events)
// ============================================================

function renderActivityTable() {
    if (!activityBody) return;

    // Collect system events, sorted newest-first
    const events = Array.from(state.systemEvents.values())
        .sort((a, b) => b.started_at - a.started_at)
        .slice(0, MAX_ACTIVITY_ROWS);

    activityBody.innerHTML = '';

    for (const ev of events) {
        const tr = document.createElement('tr');

        // Time
        const tdTime = document.createElement('td');
        tdTime.className = 'mono';
        tdTime.textContent = formatTime(ev.started_at);
        tr.appendChild(tdTime);

        // Type
        const tdType = document.createElement('td');
        tdType.textContent = ev.event_type || '\u2014';
        tr.appendChild(tdType);

        // Title
        const tdTitle = document.createElement('td');
        tdTitle.textContent = ev.title || '\u2014';
        tr.appendChild(tdTitle);

        // Status badge
        const tdStatus = document.createElement('td');
        const badge = document.createElement('span');
        badge.className = `activity-badge ${ev.status || ''}`;
        badge.textContent = ev.status || 'unknown';
        tdStatus.appendChild(badge);
        tr.appendChild(tdStatus);

        // Duration
        const tdDuration = document.createElement('td');
        tdDuration.className = 'mono';
        if (ev.ended_at && ev.started_at) {
            tdDuration.textContent = formatDuration(ev.ended_at - ev.started_at);
        } else if (ev.status === 'active') {
            tdDuration.textContent = '\u2026';
        } else {
            tdDuration.textContent = '\u2014';
        }
        tr.appendChild(tdDuration);

        activityBody.appendChild(tr);
    }

    if (events.length === 0) {
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 5;
        td.className = 'muted';
        td.style.textAlign = 'center';
        td.textContent = 'No system activity yet';
        tr.appendChild(td);
        activityBody.appendChild(tr);
    }
}

// ============================================================
// Bootstrap
// ============================================================

connectSSE();
requestAnimationFrame(render);
