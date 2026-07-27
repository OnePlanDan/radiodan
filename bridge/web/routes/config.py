"""
Config routes — read/write system configuration with live apply.
"""

import logging

from aiohttp import web

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()

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

_SERVICE_MAP = {
    "tts": ("tts_service", {"endpoint", "speaker", "language", "instruct"}),
    "stt": ("stt_service", {"endpoint"}),
}


@routes.get("/api/config")
async def get_config(request: web.Request) -> web.Response:
    """All editable config sections with current values and running defaults."""
    config_store = request.app["config_store"]
    ctx = request.app.get("ctx_kwargs", {})

    sections = {}
    for section_key, section_meta in EDITABLE_SECTIONS.items():
        stored = await config_store.get_section(section_key)
        service = ctx.get(section_meta.get("service_key", ""))

        fields = {}
        for field_key, field_meta in section_meta["fields"].items():
            running_default = ""
            if service and field_meta.get("attr"):
                running_default = str(getattr(service, field_meta["attr"], ""))

            fields[field_key] = {
                "type": field_meta["type"],
                "label": field_meta["label"],
                "value": stored.get(field_key, ""),
                "running_default": running_default,
            }

        sections[section_key] = {
            "label": section_meta["label"],
            "fields": fields,
        }

    return web.json_response({"sections": sections})


@routes.put("/api/config")
async def save_config(request: web.Request) -> web.Response:
    """Save config changes and apply live. Body: {"tts.endpoint": "...", ...}"""
    config_store = request.app["config_store"]
    ctx = request.app.get("ctx_kwargs", {})
    body = await request.json()

    changes: dict[str, dict[str, str]] = {}
    for field_name, value in body.items():
        if "." not in field_name:
            continue
        section, key = field_name.split(".", 1)
        if section not in EDITABLE_SECTIONS:
            continue
        if key not in EDITABLE_SECTIONS[section]["fields"]:
            continue

        value = str(value).strip()
        if value:
            await config_store.set(section, key, value)
        else:
            await config_store.delete(section, key)
        changes.setdefault(section, {})[key] = value

    # Apply to running services
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

    return web.json_response({"ok": True, "changes": changes})
