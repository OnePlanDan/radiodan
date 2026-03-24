"""
Library routes — stats, folder upload, rescan.
"""

import asyncio
import logging
import os
import stat
from html import escape
from pathlib import Path

import aiohttp_jinja2
from aiohttp import web

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()

AUDIO_EXTENSIONS = {".mp3", ".flac", ".ogg", ".wav", ".m4a", ".aac", ".opus", ".wma"}


@routes.get("/library")
@aiohttp_jinja2.template("library.html")
async def library_page(request: web.Request) -> dict:
    """Render the library page."""
    return {"page": "library"}


@routes.get("/api/library/stats")
async def stats_partial(request: web.Request) -> web.Response:
    """Return library stats as HTMX partial."""
    planner = request.app["ctx_kwargs"]["playlist_planner"]
    lib = planner.library

    artists = set(t.get("artist", "") for t in lib if t.get("artist"))
    albums = set(t.get("album", "") for t in lib if t.get("album"))
    total_duration = sum(t.get("duration_seconds", 0) for t in lib)
    hours = total_duration / 3600

    # Get total size via du (async, fast)
    size_label = "..."
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

    html = (
        f'<span class="badge badge-info">{len(lib)} tracks</span> '
        f'<span class="badge badge-info">{len(artists)} artists</span> '
        f'<span class="badge badge-info">{len(albums)} albums</span> '
        f'<span class="badge badge-info">{size_label}</span> '
        f'<span class="badge badge-info">{hours:.1f} hours</span>'
    )
    return web.Response(text=html, content_type="text/html")


@routes.post("/library/rescan")
async def rescan(request: web.Request) -> web.Response:
    """Trigger an immediate library rescan."""
    planner = request.app["ctx_kwargs"]["playlist_planner"]
    await planner._scan_library()
    count = len(planner.library)
    return web.Response(
        text=f'<span class="flash success">Rescan complete: {count} tracks</span>',
        content_type="text/html",
    )


@routes.post("/library/upload")
async def upload_files(request: web.Request) -> web.Response:
    """Handle multipart folder upload. Preserves directory structure."""
    planner = request.app["ctx_kwargs"]["playlist_planner"]
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
            await field.read()  # drain
            continue

        filename = field.filename
        if not filename:
            continue

        # Check extension
        ext = Path(filename).suffix.lower()
        if ext not in AUDIO_EXTENSIONS:
            skipped += 1
            await field.read()  # drain
            continue

        # Preserve relative path (sent as filename with slashes)
        # Browser sends: "Artist/Album/track.mp3"
        rel_path = Path(filename)
        target = music_dir / rel_path

        try:
            # Create directories
            target.parent.mkdir(parents=True, exist_ok=True)

            # Write file
            content = await field.read()
            target.write_bytes(content)

            # Make world-readable for Liquidsoap (uid=100)
            target.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)  # 644
            # Ensure all parent dirs are traversable
            for parent in rel_path.parents:
                d = music_dir / parent
                if d.exists() and d != music_dir:
                    d.chmod(stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)  # 755

            saved += 1
            logger.info(f"Uploaded: {rel_path}")
        except Exception:
            errors += 1
            logger.exception(f"Failed to save: {rel_path}")

    # Rescan library to pick up new files
    if saved > 0:
        await planner._scan_library()

    parts = [f"Uploaded {saved} files."]
    if skipped:
        parts.append(f"{skipped} skipped (not audio).")
    if errors:
        parts.append(f"{errors} failed.")
    if saved:
        parts.append(f"Library: {len(planner.library)} tracks.")

    return web.Response(
        text=f'<span class="flash success">{" ".join(parts)}</span>',
        content_type="text/html",
    )
