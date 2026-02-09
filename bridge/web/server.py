"""
RadioDan Web Server

aiohttp-based web admin GUI with HTMX for interactivity.
Runs on port 49995 alongside the Telegram bot.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp_jinja2
import jinja2
from aiohttp import web

if TYPE_CHECKING:
    from bridge.config_store import ConfigStore
    from bridge.event_store import EventStore
    from bridge.audio.mixer import LiquidsoapMixer
    from bridge.audio.stream_context import StreamContext
    from bridge.plugins.base import DJPlugin

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


class WebServer:
    """HTMX-based web admin GUI for RadioDan."""

    def __init__(
        self,
        config_store: "ConfigStore",
        mixer: "LiquidsoapMixer",
        stream_context: "StreamContext",
        plugins: list["DJPlugin"],
        event_store: "EventStore | None" = None,
        ctx_kwargs: dict | None = None,
        station_name: str = "Radio Dan",
        host: str = "0.0.0.0",
        port: int = 49995,
    ):
        self.config_store = config_store
        self.mixer = mixer
        self.stream_context = stream_context
        self.plugins = plugins
        self.station_name = station_name
        self.host = host
        self.port = port
        self.app = web.Application()
        self._runner: web.AppRunner | None = None

        # Store references in app for route handlers
        self.app["config_store"] = config_store
        self.app["mixer"] = mixer
        self.app["stream_context"] = stream_context
        self.app["plugins"] = plugins
        self.app["ctx_kwargs"] = ctx_kwargs or {}
        if event_store is not None:
            self.app["event_store"] = event_store

        # Set up Jinja2 templates with station_name in global context
        env = aiohttp_jinja2.setup(
            self.app,
            loader=jinja2.FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=jinja2.select_autoescape(["html"]),
        )
        env.globals["station_name"] = station_name

        # Set up routes
        self._setup_routes()

    def _setup_routes(self) -> None:
        """Register all route handlers."""
        from bridge.web.routes.dashboard import routes as dashboard_routes
        from bridge.web.routes.plugins import routes as plugin_routes
        from bridge.web.routes.audio import routes as audio_routes
        from bridge.web.routes.config import routes as config_routes
        from bridge.web.routes.timeline import routes as timeline_routes
        from bridge.web.routes.system import routes as system_routes
        from bridge.web.routes.queue import routes as queue_routes

        self.app.router.add_routes(dashboard_routes)
        self.app.router.add_routes(plugin_routes)
        self.app.router.add_routes(audio_routes)
        self.app.router.add_routes(config_routes)
        self.app.router.add_routes(timeline_routes)
        self.app.router.add_routes(system_routes)
        self.app.router.add_routes(queue_routes)

        # Static files
        self.app.router.add_static("/static", STATIC_DIR, name="static")

    def update_plugins(self, plugins: list["DJPlugin"]) -> None:
        """Update the plugin list (called after hot-reload)."""
        self.plugins = plugins
        self.app["plugins"] = plugins

    async def start(self) -> None:
        """Start the web server."""
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info(f"Web GUI started at http://{self.host}:{self.port}")

    async def stop(self) -> None:
        """Stop the web server."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            logger.info("Web GUI stopped")
