"""End-to-end tests through an MCP client session (in-memory and over real stdio)."""

import os
import shutil
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.context import RequestContext
from mcp.shared.memory import create_connected_server_and_client_session

from edwh_mcp_plugin.server import create_server


async def test_in_memory_sudo_elicitation_retry(fake_edwh, project, scenario, get_calls):
    scenario("sudo-once")
    elicitations: list[types.ElicitRequestParams] = []

    async def on_elicit(context: RequestContext, params: types.ElicitRequestParams) -> types.ElicitResult:  # noqa: ARG001
        elicitations.append(params)
        return types.ElicitResult(action="accept", content={"retry": True})

    server = create_server(executable=fake_edwh, cwd=project, test_task="test.run")
    async with create_connected_server_and_client_session(server, elicitation_callback=on_elicit) as client:
        result = await client.call_tool("test", {"args": ["-x"]})

    assert not result.isError
    assert result.structuredContent is not None
    assert result.structuredContent["ok"] is True
    assert [call["argv"] for call in get_calls()] == [["test.run", "-x"], ["test.run", "-x"]]

    (params,) = elicitations
    assert "edwh sudo" in params.message
    assert isinstance(params, types.ElicitRequestFormParams)
    assert list(params.requestedSchema["properties"]) == ["retry"]  # and no password field


async def test_in_memory_progress_heartbeat(fake_edwh, project, scenario, monkeypatch):
    scenario("slow")
    monkeypatch.setenv("FAKE_EDWH_SLEEP", "0.5")
    seen: list[tuple[float, str | None]] = []

    async def on_progress(progress: float, total: float | None, message: str | None) -> None:  # noqa: ARG001
        seen.append((progress, message))

    server = create_server(executable=fake_edwh, cwd=project, test_task=False, heartbeat_interval=0.1)
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("fmt", {}, progress_callback=on_progress)

    assert result.structuredContent is not None
    assert result.structuredContent["ok"] is True
    assert seen
    assert all("still running" in (message or "") for _, message in seen)


async def test_in_memory_sudo_without_elicitation(fake_edwh, project, scenario):
    scenario("sudo")
    server = create_server(executable=fake_edwh, cwd=project, test_task=False)
    async with create_connected_server_and_client_session(server) as client:
        result = await client.call_tool("lint", {})

    content = result.structuredContent
    assert content is not None
    assert content["ok"] is False
    assert content["error"]["type"] == "sudo_required"
    assert "edwh sudo" in content["error"]["hint"]


async def test_stdio_roundtrip(fake_edwh, project, scenario):
    scenario("lint-failure")
    code = (
        "import sys; from pathlib import Path; from edwh_mcp_plugin.server import create_server; "
        "create_server(executable=Path(sys.argv[1]), cwd=Path(sys.argv[2])).run('stdio')"
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", code, str(fake_edwh), str(project)],
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
        init = await client.initialize()
        assert init.instructions and "pyproject.toml" in init.instructions
        tools = {tool.name for tool in (await client.list_tools()).tools}
        assert tools == {"lint", "fmt", "test"}
        result = await client.call_tool("lint", {"directory": "src"})

    content = result.structuredContent
    assert content is not None
    assert content["ok"] is False
    assert "F401" in content["output_tail"]


EDWH = Path(sys.executable).with_name("edwh")


@pytest.mark.skipif(not EDWH.exists() or not shutil.which("git"), reason="needs the real edwh in this venv")
async def test_real_edwh_mcp_lists_tools():
    """`venv/bin/edwh mcp` from this repo starts a working server (with the real edwh, real task discovery)."""
    root = Path(__file__).parent.parent
    params = StdioServerParameters(command=str(EDWH), args=["mcp"], cwd=str(root), env=dict(os.environ))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
        await client.initialize()
        tools = {tool.name for tool in (await client.list_tools()).tools}
    assert {"lint", "fmt"} <= tools
