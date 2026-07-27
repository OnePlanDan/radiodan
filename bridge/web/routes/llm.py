"""
LLM discovery routes — scan LAN for Ollama instances, select models.
"""

import asyncio
import ipaddress
import logging
import socket

import aiohttp
from aiohttp import web

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()

DEFAULT_SUBNET = "192.168.1.0/24"
PROBE_TIMEOUT = 20


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


async def _probe_host(
    session: aiohttp.ClientSession, ip: str, port: int = 11434,
) -> dict | None:
    url = f"http://{ip}:{port}/api/tags"
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            models = []
            for m in data.get("models", []):
                size_bytes = m.get("size", 0)
                if size_bytes > 1_000_000_000:
                    size_label = f"{size_bytes / 1_000_000_000:.1f} GB"
                elif size_bytes > 1_000_000:
                    size_label = f"{size_bytes / 1_000_000:.0f} MB"
                else:
                    size_label = f"{size_bytes} B"
                models.append({
                    "name": m.get("name", "unknown"),
                    "size_label": size_label,
                    "param_size": m.get("details", {}).get("parameter_size", ""),
                })
            return {"ip": ip, "port": port, "models": models}
    except Exception:
        return None


async def _scan_subnet(config_store, network, known_ips, app) -> None:
    all_ips = [str(ip) for ip in network.hosts()]
    unknown_ips = [ip for ip in all_ips if ip not in known_ips]

    progress = {"scanned": 0, "total": len(unknown_ips), "found": 0}
    app["_llm_scan_progress"] = progress

    logger.info(f"LLM scan started: {network} ({len(unknown_ips)} unknown hosts)")

    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *[_probe_host(session, ip) for ip in unknown_ips],
            return_exceptions=True,
        )

    progress["scanned"] = len(unknown_ips)

    for ip, result in zip(unknown_ips, results):
        if isinstance(result, dict) and result["models"]:
            model_names = [m["name"] for m in result["models"]]
            await config_store.upsert_ollama_host(ip, 11434, model_names)
            progress["found"] += 1

    logger.info(f"LLM scan complete: {progress['found']} new hosts found")


@routes.get("/api/llm")
async def get_llm(request: web.Request) -> web.Response:
    """Current active LLM endpoint, model, system prompt."""
    config_store = request.app["config_store"]
    ctx = request.app.get("ctx_kwargs", {})

    llm_section = await config_store.get_section("llm")
    llm_service = ctx.get("llm_service")

    return web.json_response({
        "endpoint": llm_section.get("endpoint", ""),
        "model": llm_section.get("model", ""),
        "system_prompt": llm_section.get("system_prompt", ""),
        "default_system_prompt": getattr(llm_service, "system_prompt", "") if llm_service else "",
    })


@routes.get("/api/llm/hosts")
async def list_hosts(request: web.Request) -> web.Response:
    """List discovered Ollama hosts with live status."""
    config_store = request.app["config_store"]
    hosts = await config_store.list_ollama_hosts()

    if not hosts:
        return web.json_response({"hosts": []})

    # Ping all hosts for live status
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *[_probe_host(session, h["ip"], h["port"]) for h in hosts],
            return_exceptions=True,
        )

    llm_section = await config_store.get_section("llm")
    active_endpoint = llm_section.get("endpoint", "")

    host_list = []
    for host, result in zip(hosts, results):
        is_up = isinstance(result, dict)
        if is_up:
            await config_store.upsert_ollama_host(host["ip"], host["port"], result["models"])

        is_active = f"http://{host['ip']}:{host['port']}" in active_endpoint

        host_list.append({
            "ip": host["ip"],
            "port": host["port"],
            "models": result["models"] if is_up else host["models"],
            "model_count": len(result["models"]) if is_up else len(host["models"]),
            "status": "up" if is_up else "down",
            "active": is_active,
            "last_seen": host.get("last_seen", ""),
            "localhost": host["ip"] == "127.0.0.1" or host["ip"] == _get_local_ip(),
        })

    return web.json_response({"hosts": host_list})


@routes.get("/api/llm/hosts/{ip}/{port}/models")
async def list_models(request: web.Request) -> web.Response:
    """List models on a specific host (live fetch)."""
    ip = request.match_info["ip"]
    port = int(request.match_info["port"])

    async with aiohttp.ClientSession() as session:
        result = await _probe_host(session, ip, port)

    if result is None:
        raise web.HTTPServiceUnavailable(reason=f"{ip}:{port} is not responding")

    config_store = request.app["config_store"]
    await config_store.upsert_ollama_host(ip, port, result["models"])

    llm_section = await config_store.get_section("llm")
    active_model = llm_section.get("model", "")
    is_active_host = f"http://{ip}:{port}" in llm_section.get("endpoint", "")

    models = []
    for m in result["models"]:
        models.append({
            **m,
            "active": is_active_host and m["name"] == active_model,
        })

    return web.json_response({"models": models})


@routes.post("/api/llm/select")
async def select_model(request: web.Request) -> web.Response:
    """Set active LLM endpoint + model. Body: {"ip": "...", "port": 11434, "model": "..."}"""
    config_store = request.app["config_store"]
    ctx = request.app.get("ctx_kwargs", {})
    body = await request.json()

    ip = (body.get("ip") or "").strip()
    port = str(body.get("port", "11434")).strip()
    model = (body.get("model") or "").strip()

    if not ip or not model:
        raise web.HTTPBadRequest(reason="ip and model are required")

    endpoint = f"http://{ip}:{port}/v1/chat/completions"

    await config_store.set("llm", "endpoint", endpoint)
    await config_store.set("llm", "model", model)

    llm_service = ctx.get("llm_service")
    if llm_service:
        llm_service.endpoint = endpoint
        llm_service.model = model
        logger.info(f"LLM switched live: {model} on {ip}:{port}")

    return web.json_response({"ok": True, "endpoint": endpoint, "model": model})


@routes.put("/api/llm/system-prompt")
async def save_system_prompt(request: web.Request) -> web.Response:
    """Save and apply system prompt. Body: {"system_prompt": "..."}"""
    config_store = request.app["config_store"]
    ctx = request.app.get("ctx_kwargs", {})
    body = await request.json()

    prompt = (body.get("system_prompt") or "").strip()

    if prompt:
        await config_store.set("llm", "system_prompt", prompt)
    else:
        await config_store.delete("llm", "system_prompt")

    llm_service = ctx.get("llm_service")
    if llm_service and prompt:
        llm_service.system_prompt = prompt

    return web.json_response({"ok": True})


@routes.post("/api/llm/scan")
async def start_scan(request: web.Request) -> web.Response:
    """Start a background LAN scan. Body: {"subnet": "192.168.1.0/24"}"""
    config_store = request.app["config_store"]
    body = await request.json()
    subnet = (body.get("subnet") or DEFAULT_SUBNET).strip()

    try:
        network = ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        raise web.HTTPBadRequest(reason=f"Invalid subnet: {subnet}")

    await config_store.set("llm_discovery", "subnet", subnet)

    existing = request.app.get("_llm_scan_task")
    if existing and not existing.done():
        return web.json_response({"ok": False, "reason": "Scan already in progress"})

    known_hosts = await config_store.list_ollama_hosts()
    known_ips = {h["ip"] for h in known_hosts}

    task = asyncio.create_task(_scan_subnet(config_store, network, known_ips, request.app))
    request.app["_llm_scan_task"] = task

    return web.json_response({
        "ok": True,
        "subnet": subnet,
        "hosts_to_scan": network.num_addresses - 2,
    })


@routes.get("/api/llm/scan-status")
async def scan_status(request: web.Request) -> web.Response:
    """Check scan progress."""
    task = request.app.get("_llm_scan_task")
    progress = request.app.get("_llm_scan_progress", {})

    running = task is not None and not task.done()
    return web.json_response({
        "running": running,
        "scanned": progress.get("scanned", 0),
        "total": progress.get("total", 0),
        "found": progress.get("found", 0),
    })
