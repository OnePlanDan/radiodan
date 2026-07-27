"""
Status routes — GET /api/status, GET /api/status/health

Now-playing info, timing, system health.
"""

import asyncio
import logging
import os
import time

from aiohttp import web

from bridge.web.helpers import get_planner, get_service

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


def _format_uptime(seconds: float) -> str:
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, secs = divmod(s, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


@routes.get("/api/status")
async def status(request: web.Request) -> web.Response:
    """Current playback state, track info, and active plugins."""
    stream_context = request.app["stream_context"]
    plugins = request.app["plugins"]

    track = stream_context.current_track or {}
    remaining = stream_context.remaining_seconds
    elapsed = stream_context.elapsed_seconds

    # Star count
    stars = 0
    filename = track.get("filename", "")
    if filename:
        try:
            planner = get_planner(request)
            file_path = planner.resolve_file_path(filename)
            stars = await planner.star_count(file_path)
        except Exception:
            pass

    return web.json_response({
        "station_name": request.app.get("station_name", "Radio Dan"),
        "stream_url": request.app.get("stream_url", ""),
        "now_playing": {
            "artist": track.get("artist", ""),
            "title": track.get("title", ""),
            "album": track.get("album", ""),
            "genre": track.get("genre", ""),
            "year": track.get("year", ""),
            "filename": filename,
            "stars": stars,
        },
        "timing": {
            "elapsed": round(elapsed, 1),
            "remaining": round(remaining, 1),
        },
        "plugins": [
            {
                "instance_id": p.instance_id,
                "display_name": p.display_name,
                "name": p.name,
                "version": p.version,
            }
            for p in plugins
        ],
    })


def _read_self_rss_mb() -> float | None:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    return round(kb / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    return None


async def _docker_info(container: str) -> dict:
    info = {"name": container, "status": "stopped", "pid": None, "uptime": None, "memory": None}
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect",
            "--format", "{{.State.Pid}} {{.State.Status}} {{.State.StartedAt}}",
            container,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            parts = stdout.decode().strip().split(None, 2)
            if len(parts) >= 2:
                info["pid"] = int(parts[0]) if parts[0] != "0" else None
                info["status"] = parts[1]
            if len(parts) >= 3:
                from datetime import datetime, timezone
                started_str = parts[2].split(".")[0]
                started = datetime.fromisoformat(started_str).replace(tzinfo=timezone.utc)
                info["uptime"] = _format_uptime(time.time() - started.timestamp())
    except (asyncio.TimeoutError, Exception) as e:
        logger.debug(f"docker inspect {container}: {e}")

    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            info["memory"] = stdout.decode().strip().split("/")[0].strip()
    except (asyncio.TimeoutError, Exception) as e:
        logger.debug(f"docker stats {container}: {e}")

    return info


@routes.get("/api/status/health")
async def health(request: web.Request) -> web.Response:
    """Service health: bridge, liquidsoap, icecast, TTS, LLM."""
    start_time = request.app.get("start_time", time.time())
    python_info = {
        "name": "Python Bridge",
        "status": "running",
        "pid": os.getpid(),
        "uptime": _format_uptime(time.time() - start_time),
        "memory_mb": _read_self_rss_mb(),
    }

    project = os.environ.get("COMPOSE_PROJECT_NAME", "radiodan")
    icecast_info, liquidsoap_info = await asyncio.gather(
        _docker_info(f"{project}-icecast-1"),
        _docker_info(f"{project}-liquidsoap-1"),
    )
    icecast_info["name"] = "Icecast"
    liquidsoap_info["name"] = "Liquidsoap"

    # Service health checks
    ctx = request.app.get("ctx_kwargs", {})

    async def _safe_check(service):
        if not service:
            return False
        try:
            return await service.health_check()
        except Exception:
            return False

    tts_ok, llm_ok = await asyncio.gather(
        _safe_check(ctx.get("tts_service")),
        _safe_check(ctx.get("llm_service")),
    )

    return web.json_response({
        "services": {
            "python": python_info,
            "icecast": icecast_info,
            "liquidsoap": liquidsoap_info,
            "tts": {"healthy": tts_ok},
            "llm": {"healthy": llm_ok},
        },
    })
