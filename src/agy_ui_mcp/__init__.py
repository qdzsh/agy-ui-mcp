"""agy-ui-mcp: an MCP server that delegates frontend/UI work to the agy CLI.

The package drives the ``agy`` CLI (Gemini, subscription-authenticated) behind a
Model Context Protocol server so that Claude Code and Codex can hand off FE/UI
tasks. agy is spawned as ``agy -p "<prompt>"`` through a Python PTY (to dodge a
non-TTY stdout bug), and scope is enforced with a *diff-gate*: after each turn we
``git diff`` the worktree and revert any path that is not explicitly allowed.

Public surface is intentionally small; see :mod:`agy_ui_mcp.server` for the tool
definitions and :mod:`agy_ui_mcp.scope` for the config model.
"""

from __future__ import annotations

__all__ = [
    "__version__",
    "MODEL",
]

#: Package version. Kept in sync with ``pyproject.toml``.
__version__: str = "0.1.0"

#: Default model this server delegates to. Overridable via the scope file's
#: ``model`` field and the agy ``--model`` flag.
MODEL: str = "gemini-3.5-flash"
