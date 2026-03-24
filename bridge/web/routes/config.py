"""
Config routes — general system configuration editor.

Changes are saved to SQLite and applied to running services immediately.
"""

import logging

import aiohttp_jinja2
from aiohttp import web

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()

# Sections that are editable through the web GUI
# "attr" maps each field to the service attribute that holds the running default
EDITABLE_SECTIONS = {
    "tts": {
        "label": "TTS Service",
        "service_key": "tts_service",
        "fields": {
            "endpoint": {"type": "text", "label": "Endpoint URL", "attr": "endpoint"},
            "speaker": {"type": "text", "label": "Speaker", "attr": "speaker"},
            "language": {"type": "text", "label": "Language", "attr": "language"},
            "instruct": {"type": "text", "label": "Voice Instruction", "attr": "instruct"},
        },
    },
    "stt": {
        "label": "STT Service",
        "service_key": "stt_service",
        "fields": {
            "endpoint": {"type": "text", "label": "Endpoint URL", "attr": "endpoint"},
        },
    },
}


@routes.get("/config")
@aiohttp_jinja2.template("config.html")
async def config_page(request: web.Request) -> dict:
    """Render the config page with running defaults shown as placeholders."""
    config_store = request.app["config_store"]
    ctx = request.app.get("ctx_kwargs", {})

    sections = {}
    for section_key, section_meta in EDITABLE_SECTIONS.items():
        stored = await config_store.get_section(section_key)

        # Get the live service to read running defaults
        service = ctx.get(section_meta.get("service_key", ""))

        fields = {}
        for field_key, field_meta in section_meta["fields"].items():
            # Running default from the live service object
            running_default = ""
            if service and field_meta.get("attr"):
                running_default = str(getattr(service, field_meta["attr"], ""))

            fields[field_key] = {
                **field_meta,
                "value": stored.get(field_key, ""),
                "placeholder": running_default or field_meta.get("placeholder", ""),
            }
        sections[section_key] = {
            "label": section_meta["label"],
            "fields": fields,
        }

    return {
        "page": "config",
        "sections": sections,
    }


@routes.put("/config")
async def save_config(request: web.Request) -> web.Response:
    """Save config changes and apply to running services immediately."""
    config_store = request.app["config_store"]
    ctx = request.app.get("ctx_kwargs", {})
    data = await request.post()

    # Parse form fields: "section.key" → (section, key, value)
    changes: dict[str, dict[str, str]] = {}
    for field_name, value in data.items():
        if "." not in field_name:
            continue
        section, key = field_name.split(".", 1)
        if section not in EDITABLE_SECTIONS:
            continue
        if key not in EDITABLE_SECTIONS[section]["fields"]:
            continue

        value = value.strip()
        if value:
            await config_store.set(section, key, value)
        else:
            await config_store.delete(section, key)
            value = ""
        changes.setdefault(section, {})[key] = value

    # Apply changes to live services
    _apply_live(ctx, changes)

    if request.headers.get("HX-Request"):
        return web.Response(
            text='<div id="status-message" class="flash success">Settings applied!</div>',
            content_type="text/html",
        )
    raise web.HTTPSeeOther("/config")


# Map of section → service key in ctx_kwargs → attribute names
_SERVICE_MAP = {
    "tts": ("tts_service", {"endpoint", "speaker", "language", "instruct"}),
    "stt": ("stt_service", {"endpoint"}),
}


def _apply_live(ctx: dict, changes: dict[str, dict[str, str]]) -> None:
    """Push changed config values into running service objects."""
    for section, fields in changes.items():
        mapping = _SERVICE_MAP.get(section)
        if not mapping:
            continue
        service_key, allowed_attrs = mapping
        service = ctx.get(service_key)
        if not service:
            continue
        for key, value in fields.items():
            if key in allowed_attrs and hasattr(service, key):
                setattr(service, key, value)
                logger.info(f"Config applied: {section}.{key} updated live")
