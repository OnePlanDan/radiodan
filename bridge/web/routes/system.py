"""
System status & restart routes.

GET  /api/system/status   — JSON process info (HTMX polling target)
POST /system/restart       — Restart all services (detached)
POST /system/restart-docker — Restart Docker containers only
POST /system/restart-python — Restart Python bridge only (detached)
"""

import asyncio
import logging
import os
import time

import aiohttp_jinja2
from aiohttp import web

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


def _format_uptime(seconds: float) -> str:
    """Format seconds into a human-readable uptime string."""
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, secs = divmod(s, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


def _read_self_rss_mb() -> float | None:
    """Read RSS memory of current process from /proc/self/status (Linux)."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    # VmRSS:    12345 kB
                    kb = int(line.split()[1])
                    return round(kb / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass
    return None


async def _docker_info(container: str) -> dict:
    """Get PID, status, and started-at for a Docker container."""
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
                info["status"] = parts[1]  # "running", "exited", etc.
            if len(parts) >= 3:
                # Parse ISO timestamp for uptime
                from datetime import datetime, timezone
                started_str = parts[2].split(".")[0]  # Trim fractional seconds
                started = datetime.fromisoformat(started_str).replace(tzinfo=timezone.utc)
                info["uptime"] = _format_uptime(time.time() - started.timestamp())
    except (asyncio.TimeoutError, Exception) as e:
        logger.debug(f"docker inspect {container}: {e}")

    # Memory via docker stats (separate call — inspect doesn't include it)
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode == 0:
            # Output like "12.34MiB / 1.94GiB"
            info["memory"] = stdout.decode().strip().split("/")[0].strip()
    except (asyncio.TimeoutError, Exception) as e:
        logger.debug(f"docker stats {container}: {e}")

    return info


async def get_process_info(app: web.Application) -> dict:
    """Gather system process information for Python bridge + Docker containers."""
    # Python bridge info
    start_time = app.get("start_time", time.time())
    python_info = {
        "name": "Python Bridge",
        "status": "running",
        "pid": os.getpid(),
        "uptime": _format_uptime(time.time() - start_time),
        "memory": _read_self_rss_mb(),
    }

    # Docker containers in parallel — use COMPOSE_PROJECT_NAME to find containers
    project = os.environ.get("COMPOSE_PROJECT_NAME", "radiodan")
    icecast_info, liquidsoap_info = await asyncio.gather(
        _docker_info(f"{project}-icecast-1"),
        _docker_info(f"{project}-liquidsoap-1"),
    )
    icecast_info["name"] = "Icecast"
    liquidsoap_info["name"] = "Liquidsoap"

    return {
        "python": python_info,
        "icecast": icecast_info,
        "liquidsoap": liquidsoap_info,
    }


@routes.get("/system")
@aiohttp_jinja2.template("system.html")
async def system_page(request: web.Request) -> dict:
    """Render the system status page."""
    processes = await get_process_info(request.app)
    return {"page": "system", "processes": processes}


@routes.get("/api/system/status")
async def system_status_api(request: web.Request) -> web.Response:
    """Return system status as an HTMX partial (HTML table rows)."""
    procs = await get_process_info(request.app)

    rows = []
    for key in ("python", "liquidsoap", "icecast"):
        p = procs[key]
        status_class = "badge-ok" if p["status"] == "running" else "badge-err"
        status_label = p["status"].capitalize()
        pid = p["pid"] or "—"
        uptime = p["uptime"] or "—"
        mem = p["memory"]
        if isinstance(mem, float):
            mem_str = f"{mem} MB"
        elif isinstance(mem, str):
            mem_str = mem
        else:
            mem_str = "—"

        rows.append(
            f'<tr>'
            f'<td>{p["name"]}</td>'
            f'<td><span class="badge {status_class}">{status_label}</span></td>'
            f'<td class="mono">{pid}</td>'
            f'<td class="mono">{uptime}</td>'
            f'<td class="mono">{mem_str}</td>'
            f'</tr>'
        )

    html = "\n".join(rows)
    return web.Response(text=html, content_type="text/html")


@routes.post("/system/restart-docker")
async def restart_docker(request: web.Request) -> web.Response:
    """Restart Docker containers (Icecast + Liquidsoap). Python stays alive."""
    project_root = request.app.get("project_root")
    if not project_root:
        return web.Response(
            text='<span class="flash error">Project root not configured</span>',
            content_type="text/html",
        )

    async def _do_restart():
        proc = await asyncio.create_subprocess_exec(
            "docker", "compose", "restart",
            cwd=str(project_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    asyncio.create_task(_do_restart())
    logger.info("Docker container restart triggered from web UI")

    return web.Response(
        text='<span class="flash success">Restarting Docker containers...</span>',
        content_type="text/html",
    )


_RECONNECT_HTML = """
<div class="flash warning" id="reconnect-msg">
    Restarting... reconnecting in a moment.
</div>
<script>
(function() {
    var attempts = 0;
    var timer = setInterval(function() {
        attempts++;
        fetch("/", {method: "HEAD"}).then(function(r) {
            if (r.ok) { clearInterval(timer); location.reload(); }
        }).catch(function() {});
        if (attempts > 60) {
            clearInterval(timer);
            document.getElementById("reconnect-msg").textContent =
                "Could not reconnect. Please refresh manually.";
        }
    }, 2000);
})();
</script>
"""


def _open_log() -> int:
    """Open the station log file for appending, return the file descriptor."""
    station = os.environ.get("STATION", "unknown")
    return os.open(f"/tmp/radiodan-{station}.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND)


@routes.post("/system/restart-python")
async def restart_python(request: web.Request) -> web.Response:
    """Restart the Python bridge via detached run_radiodan.sh restart-pyhost."""
    project_root = request.app.get("project_root")
    if not project_root:
        return web.Response(
            text='<span class="flash error">Project root not configured</span>',
            content_type="text/html",
        )

    log_fd = _open_log()
    await asyncio.create_subprocess_exec(
        "bash", str(project_root / "run_radiodan.sh"), "restart-pyhost",
        cwd=str(project_root),
        stdout=log_fd,
        stderr=log_fd,
        start_new_session=True,
    )
    os.close(log_fd)
    logger.info("Python bridge restart triggered from web UI")

    return web.Response(text=_RECONNECT_HTML, content_type="text/html")


@routes.post("/system/restart")
async def restart_all(request: web.Request) -> web.Response:
    """Restart all services via detached run_radiodan.sh restart."""
    project_root = request.app.get("project_root")
    if not project_root:
        return web.Response(
            text='<span class="flash error">Project root not configured</span>',
            content_type="text/html",
        )

    log_fd = _open_log()
    await asyncio.create_subprocess_exec(
        "bash", str(project_root / "run_radiodan.sh"), "restart",
        cwd=str(project_root),
        stdout=log_fd,
        stderr=log_fd,
        start_new_session=True,
    )
    os.close(log_fd)
    logger.info("Full restart triggered from web UI")

    return web.Response(text=_RECONNECT_HTML, content_type="text/html")
