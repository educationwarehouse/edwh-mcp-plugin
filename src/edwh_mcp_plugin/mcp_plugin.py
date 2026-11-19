"""
`edwh mcp`: serve a small set of edwh tasks to coding agents over MCP (stdio).

Start it from the project root, from the project's virtualenv: `venv/bin/edwh mcp`.
"""

import sys
from pathlib import Path

import edwh.tasks
from edwh import task
from invoke import Context


# referenced via the module: importing the task by name would also register it as `mcp.require-virtualenv`
@task(pre=[edwh.tasks.require_virtualenv], default=True, hookable=False)
def serve(_: Context, cwd: str = "") -> None:
    """
    Start the MCP server on stdio (used by Claude Code, Codex, opencode, ...).

    Args:
        cwd: project directory to run tasks in (default: the current directory)
    """
    # imported lazily: the mcp SDK is relatively heavy and not needed for every `edwh` invocation.
    from .server import create_server

    if sys.stdin.isatty():
        print(
            "edwh mcp speaks MCP over stdio and is meant to be started by an agent (see the README); "
            "waiting for a client on stdin. Press Ctrl+C to stop.",
            file=sys.stderr,
        )

    server = create_server(cwd=Path(cwd) if cwd else None)
    server.run("stdio")
