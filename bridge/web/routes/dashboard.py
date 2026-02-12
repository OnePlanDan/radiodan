"""
Dashboard route — GET /

Shows current track, active plugin instances, star status.
Extends base.html — player persists in topbar across navigation.

Track info is lazy-loaded via HTMX to keep the initial page instant.
"""

import time as _time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp_jinja2
from aiohttp import web

routes = web.RouteTableDef()


@routes.get("/")
async def dashboard(request: web.Request) -> web.Response:
    """Render the dashboard shell (instant — no track data needed)."""
    plugins = request.app["plugins"]

    active_plugins = [
        {
            "instance_id": p.instance_id,
            "display_name": p.display_name,
            "name": p.name,
            "version": p.version,
        }
        for p in plugins
    ]

    env = aiohttp_jinja2.get_env(request.app)
    template = env.get_template("dashboard.html")
    station_name = env.globals.get("station_name", "Radio Dan")
    html = template.render(
        station_name=station_name,
        plugins=active_plugins,
        page="dashboard",
    )
    return web.Response(text=html, content_type="text/html")


@routes.get("/api/dashboard/now-playing")
async def now_playing_partial(request: web.Request) -> web.Response:
    """Return the now-playing hero content as an HTMX partial."""
    stream_context = request.app["stream_context"]

    track = stream_context.current_track or {}
    remaining = stream_context.remaining_seconds
    elapsed = stream_context.elapsed_seconds

    # Star status
    is_starred = False
    file_path = track.get("filename", "")
    if file_path:
        planner = request.app["ctx_kwargs"]["playlist_planner"]
        is_starred = await planner.is_starred(planner.resolve_file_path(file_path))

    artist = track.get("artist", "")
    title = track.get("title", "")

    if not artist:
        return web.Response(
            text='<div class="hero-empty">No track information available</div>',
            content_type="text/html",
        )

    # Build meta line
    album = track.get("album", "")
    genre = track.get("genre", "")
    year = track.get("year", "")
    meta_parts = []
    if album:
        meta_parts.append(album)
    genre_year = ""
    if genre and year:
        genre_year = f"{genre} &middot; {year}"
    elif genre:
        genre_year = genre
    elif year:
        genre_year = year
    if genre_year:
        meta_parts.append(genre_year)
    meta_line = " / ".join(meta_parts)

    # Timing
    e_min, e_sec = int(elapsed // 60), int(elapsed % 60)
    r_min, r_sec = int(remaining // 60), int(remaining % 60)

    # Star button
    if is_starred:
        star_html = (
            '<button class="star-btn starred"'
            ' hx-post="/audio/unstar"'
            ' hx-target="#star-btn"'
            ' hx-swap="innerHTML">'
            '&#x2605; Starred</button>'
        )
    else:
        star_html = (
            '<button class="star-btn"'
            ' hx-post="/audio/star"'
            ' hx-target="#star-btn"'
            ' hx-swap="innerHTML">'
            '&#x2606; Star</button>'
        )

    html = f"""
<div class="hero-artist">{artist}</div>
<div class="hero-title">{title}</div>
{"<div class='hero-meta'>" + meta_line + "</div>" if meta_line else ""}
<div class="hero-timing">{e_min}:{e_sec:02d} / -{r_min}:{r_sec:02d}</div>
<div class="hero-actions">
  <button class="btn btn-skip"
          hx-post="/audio/skip"
          hx-target="#skip-status"
          hx-swap="innerHTML">
    &#x23ED; Skip
  </button>
  <span id="star-btn">{star_html}</span>
  <span id="skip-status"></span>
</div>
"""
    return web.Response(text=html, content_type="text/html")


def _fmt_duration(seconds: float) -> str:
    """Format seconds as M:SS."""
    m, s = int(seconds // 60), int(seconds % 60)
    return f"{m}:{s:02d}"


def _fmt_time(ts: float) -> str:
    """Format a unix timestamp as local HH:MM:SS."""
    t = datetime.fromtimestamp(ts)
    return t.strftime("%H:%M:%S")


def _render_playlist_html(
    upcoming: list[dict],
    history: list[dict],
    current_artist: str,
    current_title: str,
    current_started_at: float,
    upcoming_start_times: list[float],
) -> str:
    """Build the playlist body HTML as a continuous chronological timeline.

    Order: oldest history → newest history → NOW → upcoming 1 → upcoming N
    """
    parts: list[str] = []

    # History (reversed so oldest is on top → chronological order)
    for t in reversed(history):
        artist = t.get("artist", "") or "Unknown"
        title = t.get("title", "") or "Unknown"
        dur = _fmt_duration(t.get("duration_seconds", 0) or 0)
        time_str = t.get("time_str", "")
        parts.append(
            f'<div class="pl-row pl-row-history">'
            f'<span class="pl-time">{time_str}</span>'
            f'<span class="pl-pos">&middot;</span>'
            f'<span class="pl-info"><span class="pl-title">{title}</span>'
            f'<span class="pl-artist">{artist}</span></span>'
            f'<span class="pl-dur">{dur}</span>'
            f'</div>'
        )

    # Current track
    if current_artist or current_title:
        now_time = _fmt_time(current_started_at) if current_started_at else ""
        parts.append(
            f'<div class="pl-row pl-row-current">'
            f'<span class="pl-time">{now_time}</span>'
            f'<span class="pl-pos pl-now-badge">NOW</span>'
            f'<span class="pl-info"><span class="pl-title">{current_title or "Unknown"}</span>'
            f'<span class="pl-artist">{current_artist or "Unknown"}</span></span>'
            f'<span class="pl-dur"></span>'
            f'</div>'
        )

    # Upcoming
    for i, t in enumerate(upcoming):
        dur = _fmt_duration(t.get("duration_seconds", 0) or 0)
        artist = t.get("artist", "") or "Unknown"
        title = t.get("title", "") or "Unknown"
        time_str = _fmt_time(upcoming_start_times[i]) if i < len(upcoming_start_times) else ""
        parts.append(
            f'<div class="pl-row">'
            f'<span class="pl-time">{time_str}</span>'
            f'<span class="pl-pos">{i + 1}</span>'
            f'<span class="pl-info"><span class="pl-title">{title}</span>'
            f'<span class="pl-artist">{artist}</span></span>'
            f'<span class="pl-dur">{dur}</span>'
            f'</div>'
        )

    if not parts:
        parts.append('<div class="pl-empty">No playlist data yet</div>')

    return "\n".join(parts)


@routes.get("/api/dashboard/playlist")
async def playlist_partial(request: web.Request) -> web.Response:
    """Return playlist body as an HTMX partial."""
    planner = request.app["ctx_kwargs"]["playlist_planner"]
    stream_context = request.app["stream_context"]

    upcoming = planner.upcoming  # list[dict] with artist, title, duration_seconds
    raw_history = await planner.get_history(limit=5)

    # Deduplicate: history records the now-playing track immediately on
    # advance(), so the most-recent history entry often matches the current
    # track.  Remove only the first (most recent) match so an earlier play
    # of the same track still appears in the history.
    current_filename = (stream_context.current_track or {}).get("filename", "")
    if current_filename:
        current_base = Path(current_filename).name
        for i, h in enumerate(raw_history):
            if Path(h.get("file_path", "")).name == current_base:
                raw_history = raw_history[:i] + raw_history[i + 1:]
                break

    # Join history (file_path + played_at) with library to get artist/title/duration
    library_map: dict[str, dict] = {}
    for t in planner.library:
        library_map[t["file_path"]] = t
        library_map[Path(t["file_path"]).name] = t

    history: list[dict] = []
    for h in raw_history:
        fp = h["file_path"]
        lib_track = library_map.get(fp) or library_map.get(Path(fp).name, {})
        # Parse played_at ISO timestamp to local HH:MM:SS
        time_str = ""
        played_at = h.get("played_at", "")
        if played_at:
            try:
                dt = datetime.fromisoformat(played_at)
                if dt.tzinfo is not None:
                    dt = dt.astimezone()
                time_str = dt.strftime("%H:%M:%S")
            except (ValueError, TypeError):
                pass
        history.append({
            "artist": lib_track.get("artist", ""),
            "title": lib_track.get("title", ""),
            "duration_seconds": lib_track.get("duration_seconds", 0),
            "time_str": time_str,
        })

    # Current track
    track = stream_context.current_track or {}
    current_artist = track.get("artist", "")
    current_title = track.get("title", "")

    # Compute current track start time
    now = _time.time()
    elapsed = stream_context.elapsed_seconds
    remaining = stream_context.remaining_seconds
    current_started_at = now - elapsed if elapsed > 0 else now

    # Compute predicted start times for upcoming tracks
    # First upcoming starts at now + remaining (minus crossfade)
    crossfade = planner.crossfade_duration
    cursor = now + remaining - crossfade if remaining > 0 else now
    upcoming_start_times: list[float] = []
    for t in upcoming:
        upcoming_start_times.append(cursor)
        dur = t.get("duration_seconds", 180) or 180
        cursor += dur - crossfade

    html = _render_playlist_html(
        upcoming, history, current_artist, current_title,
        current_started_at, upcoming_start_times,
    )
    return web.Response(text=html, content_type="text/html")
