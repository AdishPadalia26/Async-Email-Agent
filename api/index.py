"""Vercel entry point: serves the same FastMCP app from main.py as a serverless ASGI function."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402

_db_ready = False


async def app(scope, receive, send):
    global _db_ready
    if scope["type"] == "lifespan":
        # Ack platform lifespan events; the real lifespan runs per request below.
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    if not _db_ready:
        await asyncio.to_thread(main.init_db)
        _db_ready = True

    # Stateless + JSON: no session state between invocations. The MCP session manager can only
    # be started once per app instance, so build a fresh app per request (cheap).
    http_app = main.mcp.http_app(stateless_http=True, json_response=True)
    async with http_app.router.lifespan_context(http_app):
        await http_app(scope, receive, send)
