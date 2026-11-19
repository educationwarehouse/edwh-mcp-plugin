import json
import tempfile
import typing as t
from pathlib import Path
from types import SimpleNamespace

from mcp.server.fastmcp import Context
from pydantic import BaseModel

from edwh_mcp_plugin import responses
from edwh_mcp_plugin.runner import find_test_task, list_tasks, log_dir, run_edwh, strip_ansi
from edwh_mcp_plugin.server import EdwhTools, create_server


def tools_for(fake_edwh: Path, project: Path, **kwargs) -> EdwhTools:
    return EdwhTools(executable=fake_edwh, cwd=project, **kwargs)


# --- runner ---


def test_strip_ansi():
    assert strip_ansi("\x1b[31mred\x1b[0m \x1b]8;;http://x\x07link\x1b]8;;\x07") == "red link"
    assert strip_ansi("10%\r50%\r100%\r\nnext") == "100%\nnext"


async def test_run_success_env_and_log(fake_edwh, project, get_calls):
    run = await run_edwh("lint", ["--output", "ci"], cwd=project, executable=fake_edwh)

    assert run.ok
    assert run.exit_code == 0
    assert run.result == {"mode": "ci", "results": {"linting (ruff)": True, "type checking (ty)": True}}

    (call,) = get_calls()
    assert call["argv"] == ["lint", "--output", "ci"]
    assert call["input_mode"] == "defaults"
    assert call["no_color"] == "1"
    assert call["stdin_eof"] is True
    assert Path(call["cwd"]) == project.resolve()
    # the temporary result file is cleaned up:
    assert not Path(call["result_file"]).exists()

    # output is captured in a per-project log file under the temp dir, without ansi codes in `output`:
    assert run.log_path.parent == log_dir(project)
    assert run.log_path.is_relative_to(Path(tempfile.gettempdir()) / "edwh-mcp")
    assert run.log_path.name.endswith("-lint.log")
    assert "all good" in run.log_path.read_text()
    assert "\x1b" not in run.output
    assert "⬤ all good" in run.output


async def test_heartbeat_while_running(fake_edwh, project, scenario):
    scenario("slow")
    beats: list[float] = []

    async def heartbeat(elapsed: float) -> None:
        beats.append(elapsed)

    run = await run_edwh("test.run", [], cwd=project, executable=fake_edwh, heartbeat=heartbeat, heartbeat_interval=0.1)
    assert run.ok
    assert len(beats) >= 2
    assert beats == sorted(beats)


# --- lint ---


async def test_lint_success(fake_edwh, project, get_calls):
    response = await tools_for(fake_edwh, project).lint()

    assert response["ok"] is True
    assert response["task"] == "lint --output ci --directory ."
    assert "Lint passed" in response["summary"]
    assert "output_tail" not in response
    assert "error" not in response
    assert response["result"]["results"]["type checking (ty)"] is True
    assert Path(response["log_path"]).exists()
    assert isinstance(response["duration_s"], float)
    assert get_calls()[0]["argv"] == ["lint", "--output", "ci", "--directory", "."]


async def test_lint_args(fake_edwh, project, get_calls):
    await tools_for(fake_edwh, project).lint(directory="src", fix=True)
    assert get_calls()[0]["argv"] == ["lint", "--output", "ci", "--directory", "src", "--fix"]


async def test_lint_failure_returns_diagnostics(fake_edwh, project, scenario):
    scenario("lint-failure")
    response = await tools_for(fake_edwh, project).lint()

    assert response["ok"] is False
    assert "linting (ruff): FAILED" in response["summary"]
    assert "F401" in response["output_tail"]
    assert "unresolved-import" in response["output_tail"]
    assert "truncated" not in response["output_tail"]
    assert "error" not in response  # findings are not an error; ok/summary/result already say it failed


async def test_lint_noisy_output_is_truncated(fake_edwh, project, scenario):
    scenario("noisy")
    response = await tools_for(fake_edwh, project).lint()

    lines = response["output_tail"].splitlines()
    assert len(lines) <= responses.LINT_MAX_LINES + 1
    assert len(response["output_tail"]) <= responses.LINT_MAX_CHARS + 200
    assert lines[0].startswith("noise line 0")
    assert "output truncated" in lines[-1]
    assert response["log_path"] in lines[-1]


# --- fmt ---


async def test_fmt(fake_edwh, project, get_calls):
    response = await tools_for(fake_edwh, project).fmt(directory="src")

    assert response["ok"] is True
    assert response["summary"] == "Formatted."
    assert set(response) == {"ok", "task", "summary", "log_path", "duration_s"}
    assert get_calls()[0]["argv"] == ["fmt", "--directory", "src", "--quiet"]


# --- test ---


async def test_test_success_only_summary(fake_edwh, project, get_calls):
    response = await tools_for(fake_edwh, project).test("test.run", ["-k", "thing"])

    assert response["ok"] is True
    assert response["summary"] == "Tests passed: 10 passed in 0.12s"
    assert "output_tail" not in response
    assert get_calls()[0]["argv"] == ["test.run", "-k", "thing"]


async def test_test_failure_noisy(fake_edwh, project, scenario):
    scenario("noisy")
    response = await tools_for(fake_edwh, project).test("test.run")

    assert response["ok"] is False
    assert "1 failed, 99 passed" in response["summary"]
    tail = response["output_tail"].splitlines()
    assert tail[0].startswith("[... earlier output truncated")
    assert len(tail) <= responses.TAIL_LINES + 1
    assert "FAILED tests/test_app.py::test_thing" in response["output_tail"]
    assert "\x1b" not in response["output_tail"]
    # the task's (big) result is truncated:
    assert isinstance(response["result"], str)
    assert response["result"].endswith("[... truncated]")
    assert len(response["result"]) < 5000
    # ...but the log has everything:
    log = Path(response["log_path"]).read_text()
    assert "noise line 0 " in log
    assert "noise line 4999 " in log


# --- fallbacks ---


async def test_missing_result_file_falls_back_to_exit_code(fake_edwh, project, scenario):
    scenario("no-result-file")
    response = await tools_for(fake_edwh, project).test("test")

    assert response["ok"] is False
    assert response["error"] == {"type": "failed", "message": "`edwh test` exited with code 3", "hint": None}
    assert "RuntimeError: boom" in response["output_tail"]
    assert "result" not in response


async def test_missing_result_file_success(fake_edwh, project, scenario, monkeypatch):
    scenario("no-result-file")
    monkeypatch.setenv("FAKE_EDWH_EXIT", "0")
    response = await tools_for(fake_edwh, project).lint()
    assert response["ok"] is True
    assert response["summary"] == "Lint passed."


async def test_empty_result_file(fake_edwh, project, scenario):
    scenario("empty-result-file")
    response = await tools_for(fake_edwh, project).fmt()
    assert response["ok"] is True


# --- sudo ---


async def test_sudo_required_without_elicitation(fake_edwh, project, scenario, get_calls):
    scenario("sudo")
    response = await tools_for(fake_edwh, project).test("test.run")

    assert response["ok"] is False
    assert response["error"]["type"] == "sudo_required"
    assert response["error"]["hint"] == responses.SUDO_HINT
    assert "edwh sudo" in response["error"]["hint"]
    assert "stop" in response["summary"].lower()
    assert len(get_calls()) == 1  # no retry


class FakeContext:
    """Just enough of FastMCP's Context for the sudo flow."""

    def __init__(self, action: str, elicitation: bool = True):
        caps = SimpleNamespace(elicitation=SimpleNamespace(form=None, url=None) if elicitation else None)
        session = SimpleNamespace(client_params=SimpleNamespace(capabilities=caps))
        self.request_context = SimpleNamespace(session=session)
        self.action = action
        self.elicited: list[tuple[str, type[BaseModel]]] = []
        self.progress: list[str] = []

    async def elicit(self, message: str, schema: type[BaseModel]):
        self.elicited.append((message, schema))
        return SimpleNamespace(action=self.action, data=schema() if self.action == "accept" else None)

    async def report_progress(self, progress: float, total: float | None = None, message: str | None = None) -> None:  # noqa: ARG002 - signature of Context.report_progress
        self.progress.append(message or "")


def as_ctx(fake: FakeContext) -> Context:
    return t.cast(Context, fake)


async def test_sudo_elicitation_accept_retries(fake_edwh, project, scenario, get_calls):
    scenario("sudo-once")
    ctx = FakeContext("accept")
    response = await tools_for(fake_edwh, project).test("test.run", ctx=as_ctx(ctx))

    assert response["ok"] is True
    assert len(get_calls()) == 2
    ((message, schema),) = ctx.elicited
    assert "edwh sudo" in message
    # never ask for the password itself:
    assert not any("pass" in name.lower() for name in schema.model_fields)


async def test_sudo_elicitation_retry_still_fails(fake_edwh, project, scenario, get_calls):
    scenario("sudo")
    response = await tools_for(fake_edwh, project).fmt(ctx=as_ctx(FakeContext("accept")))

    assert response["ok"] is False
    assert response["error"]["hint"] == responses.SUDO_HINT
    assert len(get_calls()) == 2  # retried exactly once


async def test_sudo_elicitation_declined(fake_edwh, project, scenario, get_calls):
    scenario("sudo")
    response = await tools_for(fake_edwh, project).fmt(ctx=as_ctx(FakeContext("decline")))
    assert response["error"]["type"] == "sudo_required"
    assert len(get_calls()) == 1


async def test_sudo_client_without_elicitation(fake_edwh, project, scenario, get_calls):
    scenario("sudo")
    ctx = FakeContext("accept", elicitation=False)
    await tools_for(fake_edwh, project).fmt(ctx=as_ctx(ctx))
    assert ctx.elicited == []
    assert len(get_calls()) == 1


async def test_progress_heartbeat_through_context(fake_edwh, project, scenario):
    scenario("slow")
    ctx = FakeContext("accept")
    tools = tools_for(fake_edwh, project, heartbeat_interval=0.1)
    await tools.fmt(ctx=as_ctx(ctx))
    assert ctx.progress
    assert "still running" in ctx.progress[0]


# --- test task detection & registration ---


def test_find_test_task_prefers_test_run(fake_edwh, project):
    listing = list_tasks(fake_edwh, project)
    assert listing is not None  # noise before the json is skipped
    assert find_test_task(listing) == "test.run"


def test_find_test_task_falls_back_to_top_level(fake_edwh, project, scenario):
    scenario("no-test-collection")
    assert find_test_task(list_tasks(fake_edwh, project)) == "test"


def test_find_test_task_none():
    assert find_test_task(None) is None
    assert find_test_task({"tasks": [{"name": "lint"}], "collections": []}) is None
    assert find_test_task({"tasks": [], "collections": [{"name": "test", "tasks": [{"name": "other"}]}]}) is None


async def test_server_registers_tools(fake_edwh, project):
    with_test = await create_server(executable=fake_edwh, cwd=project).list_tools()
    by_name = {tool.name: tool for tool in with_test}
    assert set(by_name) == {"lint", "fmt", "test"}
    assert "edwh test.run" in (by_name["test"].description or "")
    assert "pyproject.toml" in (by_name["lint"].description or "")
    assert "ctx" not in json.dumps(by_name["lint"].inputSchema)

    without_test = await create_server(executable=fake_edwh, cwd=project, test_task=False).list_tools()
    assert {tool.name for tool in without_test} == {"lint", "fmt"}
