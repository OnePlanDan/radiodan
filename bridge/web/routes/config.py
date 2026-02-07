"""
Config routes — general system configuration editor.
"""

import json

import aiohttp_jinja2
from aiohttp import web

routes = web.RouteTableDef()

# Sections that are editable through the web GUI
EDITABLE_SECTIONS = {
    "llm": {
        "label": "LLM Service",
        "fields": {
            "endpoint": {"type": "text", "label": "Endpoint URL", "placeholder": "http://localhost:11434/v1/chat/completions"},
            "model": {"type": "text", "label": "Model", "placeholder": "gpt-oss:20b"},
            "system_prompt": {"type": "textarea", "label": "Default System Prompt"},
        },
    },
    "tts": {
        "label": "TTS Service",
        "fields": {
            "endpoint": {"type": "text", "label": "Endpoint URL", "placeholder": "http://localhost:42001/tts/custom-voice"},
            "speaker": {"type": "text", "label": "Speaker", "placeholder": "Aiden"},
            "language": {"type": "text", "label": "Language", "placeholder": "English"},
            "instruct": {"type": "text", "label": "Voice Instruction", "placeholder": "Speak calmly and clearly"},
        },
    },
    "stt": {
        "label": "STT Service",
        "fields": {
            "endpoint": {"type": "text", "label": "Endpoint URL", "placeholder": "http://localhost:5000/v1/audio/transcriptions"},
        },
    },
}


@routes.get("/config")
@aiohttp_jinja2.template("config.html")
async def config_page(request: web.Request) -> dict:
    """Render the settings page."""
    config_store = request.app["config_store"]

    # Load current overrides from SQLite
    sections = {}
    for section_key, section_meta in EDITABLE_SECTIONS.items():
        stored = await config_store.get_section(section_key)
        fields = {}
        for field_key, field_meta in section_meta["fields"].items():
            fields[field_key] = {
                **field_meta,
                "value": stored.get(field_key, ""),
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
    """Save config changes from the settings form."""
    config_store = request.app["config_store"]
    data = await request.post()

    # Parse form fields: "section.key" → (section, key, value)
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

    if request.headers.get("HX-Request"):
        return web.Response(
            text='<div id="status-message" class="flash success">Settings saved! Restart to apply.</div>',
            content_type="text/html",
        )
    raise web.HTTPSeeOther("/config")
