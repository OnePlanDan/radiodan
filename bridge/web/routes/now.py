"""
Now-playing page and album art.

ICY metadata over an MP3 stream is text-only — StreamTitle is all a player
like VLC will ever show. Album art physically cannot travel in the stream, so
it is served beside it instead: `/api/nowplaying/art` returns the current
track's embedded cover, and `/now` is a small page a phone can keep open —
artwork, track, and station stats, refreshing itself. The stream stays the
product; this is the picture window next to it.
"""

import logging
from pathlib import Path

from aiohttp import web

from bridge.web.helpers import get_planner

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


def _embedded_art(path: Path) -> tuple[bytes, str] | None:
    """The cover image embedded in an audio file, as (bytes, mime)."""
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(str(path))
        if audio is None:
            return None
        tags = getattr(audio, "tags", None)
        if tags:
            # MP3: any APIC frame (APIC:, APIC:Cover, ...)
            for key in tags.keys():
                if str(key).startswith("APIC"):
                    frame = tags[key]
                    return bytes(frame.data), frame.mime or "image/jpeg"
        # MP4/M4A: covr atoms
        covr = getattr(audio, "tags", {}) or {}
        if "covr" in covr:
            art = covr["covr"][0]
            mime = "image/png" if getattr(art, "imageformat", 14) == 14 else "image/jpeg"
            return bytes(art), mime
    except Exception:
        logger.debug(f"No embedded art readable in {path}")
    return None


@routes.get("/api/nowplaying/art")
async def nowplaying_art(request: web.Request) -> web.Response:
    """Album art embedded in the current track, or 404 if it has none."""
    stream_context = request.app["stream_context"]
    track = stream_context.current_track or {}
    filename = track.get("filename", "")
    if not filename:
        raise web.HTTPNotFound(reason="Nothing playing")

    try:
        planner = get_planner(request)
        path = planner.resolve_file_path(filename)
    except Exception:
        path = Path(filename)
    if not path or not Path(path).exists():
        raise web.HTTPNotFound(reason="Track file not found on host")

    art = _embedded_art(Path(path))
    if art is None:
        raise web.HTTPNotFound(reason="Track has no embedded art")

    data, mime = art
    return web.Response(body=data, content_type=mime, headers={"Cache-Control": "no-store"})


_NOW_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Radio Dan — Now Playing</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; min-height:100vh; display:flex; flex-direction:column; align-items:center;
         justify-content:center; background:#0c0d10; color:#e8e6e0;
         font-family: system-ui, -apple-system, sans-serif; text-align:center; padding:2rem 1rem; }
  #art { width:min(78vw, 380px); height:min(78vw, 380px); border-radius:14px; object-fit:cover;
         background:#1a1c22; box-shadow:0 12px 48px rgba(0,0,0,.6); }
  #art.empty { display:flex; align-items:center; justify-content:center; }
  h1 { font-size:1.4rem; margin:1.2rem 0 .2rem; }
  #artist { color:#9aa0ab; font-size:1.05rem; margin:0; }
  #meta { color:#5c6270; font-size:.85rem; margin-top:.5rem; }
  #station { position:fixed; top:1rem; left:0; right:0; color:#5c6270;
             font-size:.8rem; letter-spacing:.25em; text-transform:uppercase; }
  #stats { position:fixed; bottom:1rem; left:0; right:0; color:#3f4450; font-size:.75rem; }
</style>
</head>
<body>
<div id="station">Radio Dan</div>
<img id="art" alt="" src="">
<h1 id="title">–</h1>
<p id="artist"></p>
<p id="meta"></p>
<div id="stats"></div>
<script>
let lastFile = null;
async function refresh() {
  try {
    const s = await (await fetch('/api/status')).json();
    const t = s.now_playing || {};
    document.getElementById('title').textContent = t.title || 'Radio Dan';
    document.getElementById('artist').textContent = t.artist || '';
    const bits = [t.album, t.year, t.genre].filter(Boolean).join(' · ');
    document.getElementById('meta').textContent = bits;
    if (t.filename !== lastFile) {
      lastFile = t.filename;
      const img = document.getElementById('art');
      img.src = '/api/nowplaying/art?f=' + encodeURIComponent(t.filename || '') + '&t=' + Date.now();
      img.onerror = () => { img.removeAttribute('src'); };
    }
  } catch (e) {}
  try {
    const st = await (await fetch('/api/stats')).json();
    const parts = [];
    if (st.songs_played_today != null) parts.push(st.songs_played_today + ' songs today');
    if (st.listener_minutes_today != null) parts.push(st.listener_minutes_today + ' min listened today');
    if (st.disk_free_gb != null) parts.push(st.disk_free_gb + ' GB free');
    document.getElementById('stats').textContent = parts.join('  ·  ');
  } catch (e) {}
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


@routes.get("/now")
async def now_page(request: web.Request) -> web.Response:
    """A phone-friendly now-playing page: artwork, track, live station stats."""
    return web.Response(text=_NOW_PAGE, content_type="text/html")
