"""
Plugin management routes — CRUD for plugin instances.
"""

import json
import logging
import re

import aiohttp_jinja2
from aiohttp import web

from bridge.plugins import get_registry, discover_plugins
from bridge.plugins.base import PluginContext

routes = web.RouteTableDef()


def _slugify(text: str) -> str:
    """Convert text to a URL-friendly slug."""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_]+', '-', text)
    return text.strip('-')


def _prepare_config_fields(plugin_cls, instance_config: dict) -> list[dict]:
    """Merge current instance config values into the plugin's field descriptors."""
    fields = plugin_cls.config_fields()
    if not fields:
        return []

    result = []
    for field in fields:
        f = dict(field)  # shallow copy

        if f["type"] == "style_picker":
            # Determine which styles are active
            default_styles = [opt["value"] for opt in f["options"]]
            f["active_styles"] = set(instance_config.get("styles", default_styles))
            # Merge weights
            default_weights = {opt["value"]: opt["default_weight"] for opt in f["options"]}
            configured_weights = instance_config.get("style_weights", {})
            f["weights"] = {**default_weights, **configured_weights}
        elif f["key"].startswith("style_prompts."):
            # Nested key: style_prompts.intro → config["style_prompts"]["intro"]
            style_name = f["key"].split(".", 1)[1]
            prompts = instance_config.get("style_prompts", {})
            f["value"] = prompts.get(style_name, f.get("default", ""))
        else:
            f["value"] = instance_config.get(f["key"], f.get("default", ""))

        result.append(f)

    return result


def _parse_form_fields(plugin_cls, data) -> dict:
    """Parse form data back into a config dict using the plugin's field descriptors."""
    fields = plugin_cls.config_fields()
    config = {}

    for field in fields:
        key = field["key"]
        ftype = field["type"]

        if ftype == "text":
            config[key] = data.get(f"field__{key}", field.get("default", ""))

        elif ftype == "textarea":
            if key.startswith("style_prompts."):
                # Handled separately below
                continue
            config[key] = data.get(f"field__{key}", field.get("default", ""))

        elif ftype == "number":
            raw = data.get(f"field__{key}", "")
            try:
                config[key] = int(raw)
            except (ValueError, TypeError):
                config[key] = field.get("default", 0)

        elif ftype == "bool":
            config[key] = f"field__{key}" in data

        elif ftype in ("select", "datetime"):
            config[key] = data.get(f"field__{key}", field.get("default", ""))

        elif ftype == "style_picker":
            # Collect active styles and weights
            styles = []
            weights = {}
            for opt in field["options"]:
                val = opt["value"]
                if f"style__{val}" in data:
                    styles.append(val)
                try:
                    weights[val] = int(data.get(f"weight__{val}", opt["default_weight"]))
                except (ValueError, TypeError):
                    weights[val] = opt["default_weight"]
            config["styles"] = styles
            config["style_weights"] = weights

    # Collect style_prompts from prompt__ fields
    style_prompts = {}
    for field in fields:
        if field["type"] == "textarea" and field["key"].startswith("style_prompts."):
            style_name = field["key"].split(".", 1)[1]
            val = data.get(f"prompt__{style_name}", "")
            if val:
                style_prompts[style_name] = val
    if style_prompts:
        config["style_prompts"] = style_prompts

    return config


@routes.get("/plugins")
@aiohttp_jinja2.template("plugins/list.html")
async def list_plugins(request: web.Request) -> dict:
    """List all plugin types and their instances."""
    config_store = request.app["config_store"]
    plugins = request.app["plugins"]

    discover_plugins()
    registry = get_registry()

    # Get all instances from DB
    all_instances = await config_store.list_instances()

    # Group instances by plugin type
    instances_by_type: dict[str, list[dict]] = {}
    for inst in all_instances:
        instances_by_type.setdefault(inst["plugin_type"], []).append(inst)

    # Build plugin type info
    plugin_types = []
    for name, cls in sorted(registry.items()):
        plugin_types.append({
            "name": name,
            "description": cls.description,
            "version": cls.version,
            "instances": instances_by_type.get(name, []),
        })

    return {
        "page": "plugins",
        "plugin_types": plugin_types,
        "running_ids": {p.instance_id for p in plugins},
    }


@routes.post("/plugins/instances")
async def create_instance(request: web.Request) -> web.Response:
    """Create a new plugin instance."""
    config_store = request.app["config_store"]
    data = await request.post()

    plugin_type = data.get("plugin_type", "").strip()
    display_name = data.get("display_name", "").strip()

    if not plugin_type or not display_name:
        raise web.HTTPBadRequest(text="plugin_type and display_name are required")

    discover_plugins()
    registry = get_registry()
    if plugin_type not in registry:
        raise web.HTTPBadRequest(text=f"Unknown plugin type: {plugin_type}")

    instance_id = _slugify(display_name)
    if not instance_id:
        instance_id = f"{plugin_type}-{display_name[:10]}"

    # Check for ID conflict
    existing = await config_store.get_instance(instance_id)
    if existing:
        # Append a number
        i = 2
        while await config_store.get_instance(f"{instance_id}-{i}"):
            i += 1
        instance_id = f"{instance_id}-{i}"

    # Default config from YAML
    config = {}
    try:
        raw_config = data.get("config", "{}")
        config = json.loads(raw_config)
    except json.JSONDecodeError:
        pass

    await config_store.create_instance(
        instance_id=instance_id,
        plugin_type=plugin_type,
        display_name=display_name,
        config=config,
    )

    # Redirect back to plugin list (or return HTMX partial)
    if request.headers.get("HX-Request"):
        raise web.HTTPSeeOther("/plugins")
    raise web.HTTPSeeOther("/plugins")


@routes.get("/plugins/instances/{id}")
@aiohttp_jinja2.template("plugins/instance_form.html")
async def edit_instance(request: web.Request) -> dict:
    """Show the edit form for a plugin instance."""
    config_store = request.app["config_store"]
    instance_id = request.match_info["id"]

    instance = await config_store.get_instance(instance_id)
    if not instance:
        raise web.HTTPNotFound(text=f"Instance not found: {instance_id}")

    discover_plugins()
    registry = get_registry()
    plugin_cls = registry.get(instance["plugin_type"])

    # Prepare config fields with current values merged in
    config_fields = []
    if plugin_cls:
        config_fields = _prepare_config_fields(plugin_cls, instance.get("config", {}))

    return {
        "page": "plugins",
        "instance": instance,
        "plugin_description": plugin_cls.description if plugin_cls else "",
        "config_fields": config_fields,
        "config_json": json.dumps(instance.get("config", {}), indent=2),
    }


@routes.put("/plugins/instances/{id}")
async def update_instance(request: web.Request) -> web.Response:
    """Update a plugin instance config."""
    config_store = request.app["config_store"]
    instance_id = request.match_info["id"]

    instance = await config_store.get_instance(instance_id)
    if not instance:
        raise web.HTTPNotFound(text=f"Instance not found: {instance_id}")

    data = await request.post()

    updates = {}
    if "display_name" in data:
        updates["display_name"] = data["display_name"].strip()

    # Check if this plugin type has config_fields
    discover_plugins()
    registry = get_registry()
    plugin_cls = registry.get(instance["plugin_type"])

    if plugin_cls and plugin_cls.config_fields():
        # Parse structured form data
        updates["config"] = _parse_form_fields(plugin_cls, data)
    elif "config" in data:
        # Fallback: parse raw JSON
        try:
            updates["config"] = json.loads(data["config"])
        except json.JSONDecodeError:
            raise web.HTTPBadRequest(text="Invalid JSON in config")

    await config_store.update_instance(instance_id, **updates)

    # Hot-reload: restart the running plugin with new config
    plugins = request.app["plugins"]
    ctx_kwargs = request.app["ctx_kwargs"]
    reload_msg = ""

    if plugin_cls and ctx_kwargs:
        # Find and stop the old instance
        old_plugin = None
        old_index = None
        for i, p in enumerate(plugins):
            if p.instance_id == instance_id:
                old_plugin = p
                old_index = i
                break

        if old_plugin is not None:
            try:
                await old_plugin.stop()

                # Read back the saved config from DB
                saved = await config_store.get_instance(instance_id)
                new_config = saved["config"] if saved else updates.get("config", {})
                display_name = saved["display_name"] if saved else instance["display_name"]

                # Create and start fresh instance
                ctx = PluginContext(config=new_config, **ctx_kwargs)
                new_plugin = plugin_cls(ctx, instance_id=instance_id, display_name=display_name)
                await new_plugin.start()

                # Swap in the plugins list
                plugins[old_index] = new_plugin

                reload_msg = " Plugin reloaded."
            except Exception:
                logging.getLogger(__name__).exception(f"Failed to reload plugin {instance_id}")
                reload_msg = " (reload failed — restart needed)"

    if request.headers.get("HX-Request"):
        return web.Response(
            text=f'<div id="status-message" class="flash success">Saved!{reload_msg}</div>',
            content_type="text/html",
        )
    raise web.HTTPSeeOther(f"/plugins/instances/{instance_id}")


@routes.delete("/plugins/instances/{id}")
async def delete_instance(request: web.Request) -> web.Response:
    """Delete a plugin instance."""
    config_store = request.app["config_store"]
    instance_id = request.match_info["id"]

    await config_store.delete_instance(instance_id)

    if request.headers.get("HX-Request"):
        return web.Response(text="", content_type="text/html")
    raise web.HTTPSeeOther("/plugins")


@routes.post("/plugins/instances/{id}/toggle")
async def toggle_instance(request: web.Request) -> web.Response:
    """Toggle an instance's enabled state (HTMX partial)."""
    config_store = request.app["config_store"]
    instance_id = request.match_info["id"]

    instance = await config_store.get_instance(instance_id)
    if not instance:
        raise web.HTTPNotFound(text=f"Instance not found: {instance_id}")

    new_state = await config_store.toggle_instance(instance_id)

    # Return updated row partial
    instance["enabled"] = new_state
    running_ids = {p.instance_id for p in request.app["plugins"]}

    response = aiohttp_jinja2.render_template(
        "plugins/_instance_row.html",
        request,
        {"instance": instance, "running_ids": running_ids},
    )
    return response
