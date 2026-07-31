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


def _active_alerts(request: web.Request) -> list[dict]:
    """Problems worth acting on, surfaced on the main status payload.

    Anything polling /api/status sees an outage without having to know to ask a
    second endpoint — the June 2026 mute went unnoticed for 41 days partly
    because the health data existed but lived somewhere nobody looked.
    """
    alerts: list[dict] = []
    watchdog = request.app.get("voice_watchdog")
    if watchdog is not None:
        try:
            state = watchdog.status()
            if state.get("alerting"):
                alerts.append({
                    "kind": "voice_outage",
                    "severity": "error",
                    "message": (
                        f"No voice segment has reached air for {state['silent_for']}. "
                        "Music is playing but the DJ is silent."
                    ),
                    "silent_for_seconds": state["silent_for_seconds"],
                    "last_error": state.get("last_error", ""),
                })
        except Exception:
            logger.exception("Could not read voice watchdog status")
    return alerts


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
        "alerts": _active_alerts(request),
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


@routes.get("/api/listeners")
async def listeners(request: web.Request) -> web.Response:
    """Who is listening now, and which hours someone usually is."""
    tracker = request.app.get("listener_tracker")
    if tracker is None:
        return web.json_response({"available": False,
                                  "reason": "listener tracking not running"})
    try:
        return web.json_response({"available": True, **await tracker.presence()})
    except Exception:
        logger.exception("Listener presence query failed")
        raise web.HTTPInternalServerError(reason="Listener presence unavailable")


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

    # Per-endpoint TTS reachability — "tts unhealthy" is not actionable on its
    # own when voices are spread across several hosts.
    tts_info: dict = {"healthy": tts_ok}
    tts_service = ctx.get("tts_service")
    if tts_service is not None:
        try:
            tts_info["endpoints"] = await tts_service.health_report()
            tts_info["voice"] = tts_service.stats()
        except Exception:
            logger.exception("TTS health report failed")

    watchdog = request.app.get("voice_watchdog")
    if watchdog is not None:
        try:
            tts_info["watchdog"] = watchdog.status()
        except Exception:
            logger.exception("Voice watchdog status failed")

    return web.json_response({
        "alerts": _active_alerts(request),
        "services": {
            "python": python_info,
            "icecast": icecast_info,
            "liquidsoap": liquidsoap_info,
            "tts": tts_info,
            "llm": {"healthy": llm_ok},
        },
    })
