"""Remote MCP over Streamable HTTP — the hostable entrypoint (Cloud Run `pleniq-mcp`).

Serves the same MCP tools over HTTP at /mcp, protected by the API key (same WG_API_KEYS as
the REST API; X-API-Key header or `Authorization: Bearer <key>`). Binds 0.0.0.0:$PORT.

Run:  python -m warmgraph.mcp_http   (needs Python >= 3.10)
"""
from __future__ import annotations

import os

import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from warmgraph.auth import key_is_valid
from warmgraph.config import get_settings
from warmgraph.mcp_server import build_mcp


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Reject unauthenticated MCP calls when WG_API_KEYS is set (open otherwise)."""

    def __init__(self, app, keys):
        super().__init__(app)
        self.keys = keys

    async def dispatch(self, request: Request, call_next):
        if self.keys:
            auth = request.headers.get("authorization", "")
            provided = request.headers.get("x-api-key") or (
                auth[7:].strip() if auth.lower().startswith("bearer ") else None
            )
            if not key_is_valid(provided, self.keys):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


def main() -> None:
    settings = get_settings()
    mcp = build_mcp()
    app = mcp.streamable_http_app()  # Starlette app (its own session-manager lifespan)
    app.add_middleware(ApiKeyMiddleware, keys=settings.api_keys)
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))


if __name__ == "__main__":
    main()
