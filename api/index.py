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

    if b"__scope" in scope.get("query_string", b""):  # TEMP debug
        import json
        info = {k: (v.decode() if isinstance(v, bytes) else v) for k, v in scope.items() if k in ("path", "raw_path", "root_path", "query_string", "type")}
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": json.dumps(info).encode()})
        return

    if not _db_ready:
        await asyncio.to_thread(main.init_db)
        _db_ready = True

    # Stateless + JSON: no session state between invocations. The MCP session manager can only
    # be started once per app instance, so build a fresh app per request (cheap).
    http_app = main.mcp.http_app(stateless_http=True, json_response=True)
    async with http_app.router.lifespan_context(http_app):
        await http_app(scope, receive, send)
