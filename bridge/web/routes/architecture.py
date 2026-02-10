"""
Architecture route — GET /architecture

Standalone page displaying Mermaid.js architecture diagrams of the RadioDan system.
"""

from aiohttp import web

routes = web.RouteTableDef()


@routes.get("/architecture")
async def architecture_page(request: web.Request) -> web.Response:
    """Render the architecture diagrams page (extends base.html)."""
    import aiohttp_jinja2

    env = aiohttp_jinja2.get_env(request.app)
    template = env.get_template("architecture.html")
    station_name = env.globals.get("station_name", "Radio Dan")
    html = template.render(station_name=station_name, page="architecture")
    return web.Response(text=html, content_type="text/html")
