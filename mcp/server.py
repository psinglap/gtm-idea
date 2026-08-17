"""Warmgraph MCP server — STDIO entrypoint (for local use in Claude Desktop).

The remote/HTTP version is `warmgraph.mcp_http` (deployed to Cloud Run). Both share the
tool definitions in `warmgraph.mcp_server`. Requires Python >= 3.10.

Run:  python mcp/server.py   (stdio transport)
"""
from __future__ import annotations

from warmgraph.mcp_server import build_mcp

mcp = build_mcp()


if __name__ == "__main__":
    mcp.run()
