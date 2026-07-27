"""
Library routes — browse, search, stats, rescan, upload.
"""

import asyncio
import logging
import stat
from pathlib import Path

from aiohttp import web

from bridge.web.helpers import get_service

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()

AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".wav", ".m4a", ".aac", ".opus", ".wma"}


@routes.get("/api/library")
async def list_library(request: web.Request) -> web.Response:
    """List/search the music library. Query params: ?q=, ?artist=, ?genre="""
    planner = get_service(request, "playlist_planner")
    lib = planner.library

    q = (request.query.get("q") or "").lower()
    artist_filter = (request.query.get("artist") or "").lower()
    genre_filter = (request.query.get("genre") or "").lower()

    results = []
    for t in lib:
        if q and q not in (t.get("artist", "") + t.get("title", "") + t.get("album", "")).lower():
            continue
        if artist_filter and artist_filter not in (t.get("artist") or "").lower():
            continue
        if genre_filter and genre_filter not in (t.get("genre") or "").lower():
            continue
        results.append({
            "file_path": t.get("file_path", ""),
            "artist": t.get("artist", ""),
            "title": t.get("title", ""),
            "album": t.get("album", ""),
            "genre": t.get("genre", ""),
            "year": t.get("year", ""),
            "duration_seconds": t.get("duration_seconds", 0),
        })

    return web.json_response({"tracks": results, "count": len(results)})


@routes.get("/api/library/stats")
async def library_stats(request: web.Request) -> web.Response:
    """Aggregate stats: track count, artists, albums, hours, disk size, top genres."""
    planner = get_service(request, "playlist_planner")
    lib = planner.library

    artists = set(t.get("artist", "") for t in lib if t.get("artist"))
    albums = set(t.get("album", "") for t in lib if t.get("album"))
    genres = set(t.get("genre", "") for t in lib if t.get("genre"))
    total_duration = sum(t.get("duration_seconds", 0) for t in lib)

    size_label = "unknown"
    try:
        proc = await asyncio.create_subprocess_exec(
            "du", "-sh", str(planner.music_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            size_label = stdout.decode().split()[0]
    except Exception:
        pass

    # Top 10 genres for quick inspection
    counts = _genre_counts(lib)
    top_genres = counts[:10]
    untagged = sum(1 for t in lib if not (t.get("genre") or "").strip())

    return web.json_response({
        "tracks": len(lib),
        "artists": len(artists),
        "albums": len(albums),
        "genres": len(genres),
        "untagged_tracks": untagged,
        "total_hours": round(total_duration / 3600, 1),
        "disk_size": size_label,
        "top_genres": [{"genre": g, "count": n} for g, n in top_genres],
    })


@routes.get("/api/library/genres")
async def library_genres(request: web.Request) -> web.Response:
    """Return every distinct genre in the library with track count, sorted desc.

    Query params:
        ?q=<substring>   — filter genres whose name contains the substring
        ?limit=<N>       — cap to top N (default: no cap)
    """
    planner = get_service(request, "playlist_planner")
    counts = _genre_counts(planner.library)

    q = (request.query.get("q") or "").lower().strip()
    if q:
        counts = [(g, n) for g, n in counts if q in g.lower()]

    limit_str = request.query.get("limit")
    if limit_str:
        try:
            counts = counts[: max(0, int(limit_str))]
        except ValueError:
            pass

    return web.json_response({
        "genres": [{"genre": g, "count": n} for g, n in counts],
        "total_distinct": len(counts),
    })


def _genre_counts(lib: list[dict]) -> list[tuple[str, int]]:
    """Count tracks per normalised genre string, sorted desc."""
    counts: dict[str, int] = {}
    for t in lib:
        g = (t.get("genre") or "").strip().lower()
        if not g:
            continue
        counts[g] = counts.get(g, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])


@routes.post("/api/library/rescan")
async def rescan(request: web.Request) -> web.Response:
    """Trigger an immediate library rescan."""
    planner = get_service(request, "playlist_planner")
    await planner._scan_library()
    return web.json_response({"ok": True, "tracks": len(planner.library)})


@routes.post("/api/library/upload")
async def upload_files(request: web.Request) -> web.Response:
    """Handle multipart folder upload. Preserves directory structure."""
    planner = get_service(request, "playlist_planner")
    music_dir = planner.music_dir

    reader = await request.multipart()
    saved = 0
    skipped = 0
    errors = 0

    while True:
        field = await reader.next()
        if field is None:
            break
        if field.name != "files":
            await field.read()
            continue

        filename = field.filename
        if not filename:
            continue

        ext = Path(filename).suffix.lower()
        if ext not in AUDIO_EXTENSIONS:
            skipped += 1
            await field.read()
            continue

        rel_path = Path(filename)
        target = music_dir / rel_path

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            content = await field.read()
            target.write_bytes(content)
            target.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
            for parent in rel_path.parents:
                d = music_dir / parent
                if d.exists() and d != music_dir:
                    d.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
            saved += 1
        except Exception:
            errors += 1
            logger.exception(f"Failed to save: {rel_path}")

    if saved > 0:
        await planner._scan_library()

    return web.json_response({
        "ok": True,
        "saved": saved,
        "skipped": skipped,
        "errors": errors,
        "library_tracks": len(planner.library),
    })
