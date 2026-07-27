"""
Plugin management routes — CRUD for plugin instances.
"""

import json
import logging
import re

from aiohttp import web

from bridge.plugins import get_registry, discover_plugins
from bridge.plugins.base import PluginContext

logger = logging.getLogger(__name__)
routes = web.RouteTableDef()


def _slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_]+', '-', text)
    return text.strip('-')


@routes.get("/api/plugins")
async def list_plugins(request: web.Request) -> web.Response:
    """List all plugin types and their instances."""
    config_store = request.app["config_store"]
    plugins = request.app["plugins"]

    discover_plugins()
    registry = get_registry()
    all_instances = await config_store.list_instances()

    instances_by_type: dict[str, list[dict]] = {}
    for inst in all_instances:
        instances_by_type.setdefault(inst["plugin_type"], []).append(inst)

    running_ids = {p.instance_id for p in plugins}

    plugin_types = []
    for name, cls in sorted(registry.items()):
        instances = instances_by_type.get(name, [])
        for inst in instances:
            inst["running"] = inst["id"] in running_ids
        plugin_types.append({
            "name": name,
            "description": cls.description,
            "version": cls.version,
            "instances": instances,
        })

    return web.json_response({"plugin_types": plugin_types})


@routes.post("/api/plugins")
async def create_instance(request: web.Request) -> web.Response:
    """Create a new plugin instance. Body: {"plugin_type": "...", "display_name": "...", "config": {}}"""
    config_store = request.app["config_store"]
    body = await request.json()

    plugin_type = (body.get("plugin_type") or "").strip()
    display_name = (body.get("display_name") or "").strip()

    if not plugin_type or not display_name:
        raise web.HTTPBadRequest(reason="plugin_type and display_name are required")

    discover_plugins()
    registry = get_registry()
    if plugin_type not in registry:
        raise web.HTTPBadRequest(reason=f"Unknown plugin type: {plugin_type}")

    instance_id = _slugify(display_name)
    if not instance_id:
        instance_id = f"{plugin_type}-{display_name[:10]}"

    existing = await config_store.get_instance(instance_id)
    if existing:
        i = 2
        while await config_store.get_instance(f"{instance_id}-{i}"):
            i += 1
        instance_id = f"{instance_id}-{i}"

    config = body.get("config", {})

    await config_store.create_instance(
        instance_id=instance_id,
        plugin_type=plugin_type,
        display_name=display_name,
        config=config,
    )

    # Start live
    ctx_kwargs = request.app.get("ctx_kwargs", {})
    plugins = request.app["plugins"]
    started = False
    if ctx_kwargs:
        try:
            ctx = PluginContext(config=config, **ctx_kwargs)
            plugin = registry[plugin_type](ctx, instance_id=instance_id, display_name=display_name)
            await plugin.start()
            plugins.append(plugin)
            started = True
        except Exception:
            logger.exception(f"Failed to start new plugin {instance_id}")

    return web.json_response({
        "ok": True,
        "instance_id": instance_id,
        "started": started,
    }, status=201)


@routes.get("/api/plugins/{id}")
async def get_instance(request: web.Request) -> web.Response:
    """Get instance detail + config field descriptors."""
    config_store = request.app["config_store"]
    instance_id = request.match_info["id"]

    instance = await config_store.get_instance(instance_id)
    if not instance:
        raise web.HTTPNotFound(reason=f"Instance not found: {instance_id}")

    discover_plugins()
    registry = get_registry()
    plugin_cls = registry.get(instance["plugin_type"])

    config_fields = plugin_cls.config_fields() if plugin_cls else []

    plugins = request.app["plugins"]
    running = any(p.instance_id == instance_id for p in plugins)

    return web.json_response({
        "instance": instance,
        "config_fields": config_fields,
        "running": running,
    })


@routes.put("/api/plugins/{id}")
async def update_instance(request: web.Request) -> web.Response:
    """Update instance config. Body: {"display_name": "...", "config": {...}}"""
    config_store = request.app["config_store"]
    instance_id = request.match_info["id"]

    instance = await config_store.get_instance(instance_id)
    if not instance:
        raise web.HTTPNotFound(reason=f"Instance not found: {instance_id}")

    body = await request.json()
    updates = {}
    if "display_name" in body:
        updates["display_name"] = body["display_name"].strip()
    if "config" in body:
        updates["config"] = body["config"]

    await config_store.update_instance(instance_id, **updates)

    # Hot-reload running plugin
    plugins = request.app["plugins"]
    ctx_kwargs = request.app.get("ctx_kwargs", {})
    reloaded = False

    discover_plugins()
    registry = get_registry()
    plugin_cls = registry.get(instance["plugin_type"])

    if plugin_cls and ctx_kwargs:
        for i, p in enumerate(plugins):
            if p.instance_id == instance_id:
                try:
                    await p.stop()
                    saved = await config_store.get_instance(instance_id)
                    new_config = saved["config"] if saved else updates.get("config", {})
                    display_name = saved["display_name"] if saved else instance["display_name"]
                    ctx = PluginContext(config=new_config, **ctx_kwargs)
                    new_plugin = plugin_cls(ctx, instance_id=instance_id, display_name=display_name)
                    await new_plugin.start()
                    plugins[i] = new_plugin
                    reloaded = True
                except Exception:
                    logger.exception(f"Failed to reload plugin {instance_id}")
                break

    return web.json_response({"ok": True, "reloaded": reloaded})


@routes.delete("/api/plugins/{id}")
async def delete_instance(request: web.Request) -> web.Response:
    """Delete a plugin instance — stops it if running."""
    config_store = request.app["config_store"]
    plugins = request.app["plugins"]
    instance_id = request.match_info["id"]

    for i, p in enumerate(plugins):
        if p.instance_id == instance_id:
            await p.stop()
            plugins.pop(i)
            break

    await config_store.delete_instance(instance_id)
    return web.json_response({"ok": True})


@routes.post("/api/plugins/{id}/toggle")
async def toggle_instance(request: web.Request) -> web.Response:
    """Toggle enabled/disabled — starts or stops plugin live."""
    config_store = request.app["config_store"]
    plugins = request.app["plugins"]
    ctx_kwargs = request.app.get("ctx_kwargs", {})
    instance_id = request.match_info["id"]

    instance = await config_store.get_instance(instance_id)
    if not instance:
        raise web.HTTPNotFound(reason=f"Instance not found: {instance_id}")

    new_state = await config_store.toggle_instance(instance_id)

    if not new_state:
        for i, p in enumerate(plugins):
            if p.instance_id == instance_id:
                await p.stop()
                plugins.pop(i)
                break
    else:
        discover_plugins()
        registry = get_registry()
        plugin_cls = registry.get(instance["plugin_type"])
        if plugin_cls and ctx_kwargs:
            try:
                ctx = PluginContext(config=instance["config"], **ctx_kwargs)
                plugin = plugin_cls(ctx, instance_id=instance_id, display_name=instance["display_name"])
                await plugin.start()
                plugins.append(plugin)
            except Exception:
                logger.exception(f"Failed to start plugin {instance_id}")

    return web.json_response({"ok": True, "enabled": new_state})
