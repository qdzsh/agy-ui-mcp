"""Entry point: ``python -m agy_ui_mcp`` starts the MCP server over stdio.

This is what Claude Code (`claude mcp add agy-ui -- python -m agy_ui_mcp`) and
Codex (`~/.codex/config.toml`) launch. It runs the FastMCP server on the stdio
transport and blocks until the client disconnects.
"""

from __future__ import annotations

from .server import get_server


def main() -> None:
    """Run the agy-ui MCP server on the stdio transport.

    Blocks until the MCP client closes the connection. No API key is required
    to start the server; only the (stubbed) Gemini turns would need one.
    """
    server = get_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
