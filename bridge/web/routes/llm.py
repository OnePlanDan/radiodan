"""
LLM server discovery routes — scan LAN for Ollama instances.

Provides a UI to discover, list, and select Ollama hosts and models
on the local network. Scan results persist in SQLite so known hosts
are checked first on subsequent visits.
"""

import asyncio
import ipaddress
import logging
import socket
from html import escape

import aiohttp
import aiohttp_jinja2
from aiohttp import web

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()

# Default scan settings
DEFAULT_SUBNET = "192.168.1.0/24"
PROBE_TIMEOUT = 20  # seconds per host (needs headroom for 254 parallel probes)


# ═══════════════════════════════════════════════════════════════════════════════
# Pages
# ═══════════════════════════════════════════════════════════════════════════════


@routes.get("/llm")
@aiohttp_jinja2.template("llm.html")
async def llm_page(request: web.Request) -> dict:
    """Render the LLM discovery page."""
    config_store = request.app["config_store"]

    ctx = request.app.get("ctx_kwargs", {})

    # Current active LLM config
    llm_section = await config_store.get_section("llm")
    active_endpoint = llm_section.get("endpoint", "")
    active_model = llm_section.get("model", "")

    # System prompt: SQLite override vs running default
    system_prompt_override = llm_section.get("system_prompt", "")
    llm_service = ctx.get("llm_service")
    default_system_prompt = getattr(llm_service, "system_prompt", "") if llm_service else ""

    # Saved default subnet (or use default)
    subnet = await config_store.get("llm_discovery", "subnet", default=DEFAULT_SUBNET)

    return {
        "page": "llm",
        "active_endpoint": active_endpoint,
        "active_model": active_model,
        "default_subnet": subnet,
        "system_prompt_override": system_prompt_override,
        "default_system_prompt": default_system_prompt,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Host table (HTMX partials)
# ═══════════════════════════════════════════════════════════════════════════════


def _get_local_ip() -> str:
    """Get this machine's LAN IP."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return ""


def _merge_localhost_hosts(hosts: list[dict]) -> list[dict]:
    """Merge 127.0.0.1 and the local LAN IP into single display rows.

    Both DB entries are kept. The merged row shows the LAN IP with a
    '(localhost)' tag and tracks both IPs so the model view can try both.
    """
    local_ip = _get_local_ip()
    if not local_ip:
        return hosts

    # Group by port — find pairs of (127.0.0.1, local_ip) on the same port
    local_entries = {}  # port → host dict
    lan_entries = {}    # port → host dict
    other = []

    for h in hosts:
        if h["ip"] == "127.0.0.1":
            local_entries[h["port"]] = h
        elif h["ip"] == local_ip:
            lan_entries[h["port"]] = h
        else:
            other.append(h)

    merged = []
    seen_ports = set()

    # Merge matching pairs
    for port in set(local_entries) | set(lan_entries):
        seen_ports.add(port)
        lan = lan_entries.get(port)
        lo = local_entries.get(port)
        # Prefer LAN entry as the primary display
        primary = lan or lo
        entry = {**primary, "_localhost": True, "_all_ips": []}
        if lan:
            entry["_all_ips"].append(lan["ip"])
        if lo:
            entry["_all_ips"].append(lo["ip"])
        merged.append(entry)

    return merged + other


@routes.get("/api/llm/hosts")
async def hosts_partial(request: web.Request) -> web.Response:
    """Return server table rows with live up/down status."""
    config_store = request.app["config_store"]
    hosts = await config_store.list_ollama_hosts()

    if not hosts:
        return web.Response(
            text='<tr><td colspan="5" class="muted" style="text-align:center;padding:1.5rem">'
                 'No servers discovered yet. Enter a subnet and click Scan LAN.</td></tr>',
            content_type="text/html",
        )

    # Merge localhost entries for display
    display_hosts = _merge_localhost_hosts(hosts)

    # Ping all display hosts in parallel for live status
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *[_probe_host(session, h["ip"], h["port"]) for h in display_hosts],
            return_exceptions=True,
        )

    # Build current active endpoint for highlighting
    llm_section = await config_store.get_section("llm")
    active_endpoint = llm_section.get("endpoint", "")

    rows = []
    for host, result in zip(display_hosts, results):
        if isinstance(result, dict):
            status = '<span class="badge badge-ok">Up</span>'
            model_count = len(result["models"])
            # Update DB with fresh model list for all IPs
            for ip in host.get("_all_ips", [host["ip"]]):
                await config_store.upsert_ollama_host(
                    ip, host["port"], result["models"],
                )
        else:
            status = '<span class="badge badge-err">Down</span>'
            model_count = len(host["models"])

        # Check if any of this host's IPs are active
        all_ips = host.get("_all_ips", [host["ip"]])
        is_active = any(f"http://{ip}:{host['port']}" in active_endpoint for ip in all_ips)
        active_cls = ' class="row-active"' if is_active else ""

        # Display label
        ip_label = host["ip"]
        if host.get("_localhost"):
            ip_label += ' <span class="muted">(localhost)</span>'

        rows.append(
            f'<tr{active_cls} style="cursor:pointer" '
            f'hx-get="/api/llm/hosts/{host["ip"]}/{host["port"]}/models" '
            f'hx-target="#model-table-body" hx-swap="innerHTML">'
            f'<td>{ip_label}</td>'
            f'<td>{host["port"]}</td>'
            f'<td>{model_count}</td>'
            f'<td>{status}</td>'
            f'<td>{host["last_seen"] or "—"}</td>'
            f'</tr>'
        )

    return web.Response(text="\n".join(rows), content_type="text/html")


# ═══════════════════════════════════════════════════════════════════════════════
# Models table (click a server → see its models)
# ═══════════════════════════════════════════════════════════════════════════════


@routes.get("/api/llm/hosts/{ip}/{port}/models")
async def models_partial(request: web.Request) -> web.Response:
    """Fetch and return model table rows for a specific host."""
    ip = request.match_info["ip"]
    port = int(request.match_info["port"])

    # Live-fetch models from the host
    async with aiohttp.ClientSession() as session:
        result = await _probe_host(session, ip, port)

    if result is None:
        return web.Response(
            text=f'<tr><td colspan="3" class="muted" style="text-align:center;padding:1rem">'
                 f'{escape(ip)}:{port} is not responding</td></tr>',
            content_type="text/html",
        )

    # Update DB
    config_store = request.app["config_store"]
    await config_store.upsert_ollama_host(ip, port, result["models"])

    if not result["models"]:
        return web.Response(
            text='<tr><td colspan="3" class="muted" style="text-align:center;padding:1rem">'
                 'No models found on this server</td></tr>',
            content_type="text/html",
        )

    # Current active model for highlighting
    llm_section = await config_store.get_section("llm")
    active_endpoint = llm_section.get("endpoint", "")
    active_model = llm_section.get("model", "")
    is_active_host = f"http://{ip}:{port}" in active_endpoint

    rows = []
    for model in result["models"]:
        name = model["name"]
        size = model.get("size_label", "")
        param = model.get("param_size", "")
        is_active = is_active_host and name == active_model
        active_cls = ' class="row-active"' if is_active else ""

        rows.append(
            f'<tr{active_cls}>'
            f'<td>{escape(name)}</td>'
            f'<td>{escape(param)}</td>'
            f'<td>{escape(size)}</td>'
            f'<td>'
            f'<button class="btn btn-secondary btn-sm" '
            f'hx-post="/llm/select" '
            f'hx-vals=\'{{"ip":"{ip}","port":"{port}","model":"{escape(name)}"}}\' '
            f'hx-target="#llm-status" hx-swap="innerHTML">'
            f'{"Active" if is_active else "Use"}</button>'
            f'</td>'
            f'</tr>'
        )

    return web.Response(text="\n".join(rows), content_type="text/html")


# ═══════════════════════════════════════════════════════════════════════════════
# Select a model (apply live)
# ═══════════════════════════════════════════════════════════════════════════════


@routes.post("/llm/select")
async def select_model(request: web.Request) -> web.Response:
    """Set the active LLM endpoint + model. Applied live, no restart."""
    config_store = request.app["config_store"]
    ctx = request.app.get("ctx_kwargs", {})
    data = await request.post()

    ip = data.get("ip", "").strip()
    port = data.get("port", "11434").strip()
    model = data.get("model", "").strip()

    if not ip or not model:
        return web.Response(
            text='<span class="flash error">Missing host or model</span>',
            content_type="text/html",
        )

    endpoint = f"http://{ip}:{port}/v1/chat/completions"

    # Save to config store
    await config_store.set("llm", "endpoint", endpoint)
    await config_store.set("llm", "model", model)

    # Apply live to running LLM service
    llm_service = ctx.get("llm_service")
    if llm_service:
        llm_service.endpoint = endpoint
        llm_service.model = model
        logger.info(f"LLM switched live: {model} on {ip}:{port}")

    return web.Response(
        text=f'<span class="flash success">Now using {escape(model)} on {escape(ip)}</span>',
        content_type="text/html",
    )


@routes.post("/llm/system-prompt")
async def save_system_prompt(request: web.Request) -> web.Response:
    """Save and apply the web chat system prompt."""
    config_store = request.app["config_store"]
    ctx = request.app.get("ctx_kwargs", {})
    data = await request.post()

    prompt = (data.get("system_prompt") or "").strip()

    if prompt:
        await config_store.set("llm", "system_prompt", prompt)
    else:
        await config_store.delete("llm", "system_prompt")

    # Apply live
    llm_service = ctx.get("llm_service")
    if llm_service and prompt:
        llm_service.system_prompt = prompt
        logger.info("LLM system prompt updated live")

    return web.Response(
        text='<span class="flash success">Saved</span>',
        content_type="text/html",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# LAN Scan
# ═══════════════════════════════════════════════════════════════════════════════


@routes.post("/llm/scan")
async def start_scan(request: web.Request) -> web.Response:
    """Kick off a background LAN scan for Ollama hosts."""
    config_store = request.app["config_store"]
    data = await request.post()
    subnet = (data.get("subnet") or DEFAULT_SUBNET).strip()

    # Validate subnet
    try:
        network = ipaddress.ip_network(subnet, strict=False)
    except ValueError:
        return web.Response(
            text=f'<span class="flash error">Invalid subnet: {escape(subnet)}</span>',
            content_type="text/html",
        )

    # Save subnet preference
    await config_store.set("llm_discovery", "subnet", subnet)

    # Don't start a new scan if one is already running
    existing = request.app.get("_llm_scan_task")
    if existing and not existing.done():
        return web.Response(
            text='<span class="flash warning">Scan already in progress</span>',
            content_type="text/html",
        )

    # Get known hosts to skip during sweep
    known_hosts = await config_store.list_ollama_hosts()
    known_ips = {h["ip"] for h in known_hosts}

    # Start background scan
    task = asyncio.create_task(
        _scan_subnet(config_store, network, known_ips, request.app)
    )
    request.app["_llm_scan_task"] = task

    host_count = network.num_addresses - 2  # exclude network + broadcast
    return web.Response(
        text=f'<span class="flash success">Scanning {subnet} ({host_count} hosts)...</span>',
        content_type="text/html",
    )


@routes.get("/api/llm/scan-status")
async def scan_status(request: web.Request) -> web.Response:
    """Return current scan progress as HTMX partial."""
    task = request.app.get("_llm_scan_task")
    progress = request.app.get("_llm_scan_progress", {})

    if task and not task.done():
        scanned = progress.get("scanned", 0)
        total = progress.get("total", 0)
        found = progress.get("found", 0)
        return web.Response(
            text=f'<span class="badge badge-info">Scanning: {scanned}/{total} hosts, {found} found</span>',
            content_type="text/html",
        )
    return web.Response(text="", content_type="text/html")


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════


async def _probe_host(
    session: aiohttp.ClientSession, ip: str, port: int = 11434,
) -> dict | None:
    """Probe a single host for Ollama API. Returns dict with models or None."""
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


async def _scan_subnet(
    config_store,
    network: ipaddress.IPv4Network,
    known_ips: set[str],
    app: web.Application,
) -> None:
    """Background task: probe all unknown IPs in parallel."""
    all_ips = [str(ip) for ip in network.hosts()]
    unknown_ips = [ip for ip in all_ips if ip not in known_ips]

    progress = {"scanned": 0, "total": len(unknown_ips), "found": 0}
    app["_llm_scan_progress"] = progress

    logger.info(f"LLM scan started: {network} ({len(unknown_ips)} unknown hosts, {len(known_ips)} known)")

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
            logger.info(f"LLM scan: found Ollama on {ip} ({len(model_names)} models)")

    logger.info(f"LLM scan complete: {progress['found']} new hosts found")
