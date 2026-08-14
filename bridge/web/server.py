"""
RadioDan API Server

JSON REST API on port 49997 for programmatic control.
"""

import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from bridge.config_store import ConfigStore
    from bridge.event_store import EventStore
    from bridge.audio.mixer import LiquidsoapMixer
    from bridge.audio.stream_context import StreamContext
    from bridge.plugins.base import DJPlugin

logger = logging.getLogger(__name__)


@web.middleware
async def json_error_middleware(request: web.Request, handler):
    """Catch exceptions and return JSON error responses."""
    try:
        return await handler(request)
    except web.HTTPException as ex:
        return web.json_response(
            {"error": ex.reason, "status": ex.status},
            status=ex.status,
        )
    except Exception:
        logger.exception("Unhandled error in request handler")
        return web.json_response(
            {"error": "Internal server error", "status": 500},
            status=500,
        )


@web.middleware
async def cors_middleware(request: web.Request, handler):
    """Allow cross-origin requests from any origin."""
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


class WebServer:
    """JSON REST API server for RadioDan."""

    def __init__(
        self,
        config_store: "ConfigStore",
        mixer: "LiquidsoapMixer",
        stream_context: "StreamContext",
        plugins: list["DJPlugin"],
        event_store: "EventStore | None" = None,
        ctx_kwargs: dict | None = None,
        station_name: str = "Radio Dan",
        stream_url: str = "",
        project_root: Path | None = None,
        voice_watchdog: object | None = None,
        listener_tracker: object | None = None,
        commissions: object | None = None,
        greeter: object | None = None,
        station_stats: object | None = None,
        host: str = "0.0.0.0",
        port: int = 49997,
    ):
        self.host = host
        self.port = port
        self.app = web.Application(
            middlewares=[json_error_middleware, cors_middleware],
            client_max_size=200 * 1024 * 1024,  # 200MB for uploads
        )
        self._runner: web.AppRunner | None = None

        # Store references for route handlers
        self.app["config_store"] = config_store
        self.app["mixer"] = mixer
        self.app["stream_context"] = stream_context
        self.app["plugins"] = plugins
        self.app["ctx_kwargs"] = ctx_kwargs or {}
        self.app["station_name"] = station_name
        self.app["stream_url"] = stream_url
        self.app["start_time"] = time.time()
        # Kept out of ctx_kwargs on purpose: that dict is splatted into
        # PluginContext, which is a fixed-field dataclass, and the watchdog is
        # not a plugin concern.
        self.app["voice_watchdog"] = voice_watchdog
        self.app["listener_tracker"] = listener_tracker
        self.app["commissions"] = commissions
        self.app["greeter"] = greeter
        self.app["station_stats"] = station_stats
        if event_store is not None:
            self.app["event_store"] = event_store
        if project_root is not None:
            self.app["project_root"] = project_root

        self._setup_routes()

    def _setup_routes(self) -> None:
        """Register all API route handlers."""
        from bridge.web.routes.status import routes as status_routes
        from bridge.web.routes.playback import routes as playback_routes
        from bridge.web.routes.queue import routes as queue_routes
        from bridge.web.routes.library import routes as library_routes
        from bridge.web.routes.config import routes as config_routes
        from bridge.web.routes.plugins import routes as plugin_routes
        from bridge.web.routes.events import routes as event_routes
        from bridge.web.routes.llm import routes as llm_routes
        from bridge.web.routes.producer import routes as producer_routes
        from bridge.web.routes.programmes import routes as programme_routes
        from bridge.web.routes.greeter import routes as greeter_routes
        from bridge.web.routes.now import routes as now_routes
        from bridge.web.routes.design import routes as design_routes
        from bridge.web.routes.index import routes as index_routes

        self.app.router.add_routes(status_routes)
        self.app.router.add_routes(playback_routes)
        self.app.router.add_routes(queue_routes)
        self.app.router.add_routes(library_routes)
        self.app.router.add_routes(config_routes)
        self.app.router.add_routes(plugin_routes)
        self.app.router.add_routes(event_routes)
        self.app.router.add_routes(llm_routes)
        self.app.router.add_routes(producer_routes)
        self.app.router.add_routes(programme_routes)
        self.app.router.add_routes(greeter_routes)
        self.app.router.add_routes(now_routes)
        self.app.router.add_routes(design_routes)
        # Last: the index reads the finished route table to describe itself.
        self.app.router.add_routes(index_routes)

    async def start(self) -> None:
        """Start the API server."""
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info(f"API server started at http://{self.host}:{self.port}")

    async def stop(self) -> None:
        """Stop the API server."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            logger.info("API server stopped")
