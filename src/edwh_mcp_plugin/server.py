"""The MCP (stdio) server: decides which edwh tasks agents get and how their results are presented."""

import typing as t
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import responses
from .runner import (
    HEARTBEAT_INTERVAL,
    RunResult,
    default_executable,
    find_test_task,
    list_tasks,
    run_edwh,
)

INSTRUCTIONS = """\
Tools for this project's `edwh` tasks. They run inside the project's virtualenv, capture all noisy output
(docker, passing tests) into a log file and return a small structured result.

- Use these tools instead of running ruff, ty, pytest or edwh by hand: running them outside the project venv gives
  wrong results (for example unresolved imports in ty).
- Fix the reported problems in the code. Never edit lint/type-check configuration in pyproject.toml and never add
  ignore comments (`# noqa`, `# type: ignore`, `# ty: ignore`) just to make a check pass.
- If a result has `error.type` "sudo_required" or "virtualenv_required": stop and ask the human, following
  `error.hint`. Do not try to work around it.
- Every tool returns `{ok, task, summary, log_path, duration_s}`, plus `output_tail` (diagnostics or the end of the
  output) and `error` (`{type, message, hint}`) on failure, and `result` when the task returned something useful.
- `log_path` points to the full output if you really need more context; prefer searching it over reading it whole.
"""

NEVER_WORK_AROUND = (
    " Use this instead of running ruff/ty/pytest/edwh by hand. Never edit pyproject.toml lint configuration or add "
    "ignore comments to make it pass; fix the code. If `error.type` is sudo_required or virtualenv_required, stop and "
    "ask the human (see `error.hint`)."
)

SUDO_ELICIT_MESSAGE = (
    "This task needs sudo. Run `edwh sudo` in a terminal to store the sudo password in the keyring, "
    "then confirm to retry."
)


# Structured result of every tool. Plain dict (not a TypedDict) on purpose: FastMCP fills missing optional keys with
# null and then fails its own output validation.
ToolResponse = dict[str, t.Any]


class SudoRetry(BaseModel):
    """Form for the sudo confirmation. Deliberately has no password field: secrets never go through MCP."""

    retry: bool = Field(default=True, description="I ran `edwh sudo` in a terminal; retry the task now.")


def supports_form_elicitation(ctx: Context | None) -> bool:
    if ctx is None:
        return False
    try:
        params = ctx.request_context.session.client_params
    except (ValueError, AttributeError):
        return False
    elicitation = params.capabilities.elicitation if params else None
    if elicitation is None:
        return False
    # an empty `elicitation: {}` capability means form mode (backwards compatibility with older spec versions)
    return getattr(elicitation, "form", None) is not None or getattr(elicitation, "url", None) is None


async def ask_sudo_retry(ctx: Context | None) -> bool:
    """Ask the human (not the model) to set up `edwh sudo`. Returns True when they confirmed a retry."""
    if not supports_form_elicitation(ctx):
        return False
    assert ctx is not None
    try:
        answer = await ctx.elicit(message=SUDO_ELICIT_MESSAGE, schema=SudoRetry)
    except Exception:
        return False
    return answer.action == "accept" and bool(getattr(answer.data, "retry", True))


class EdwhTools:
    """The tool implementations, independent of FastMCP registration so they can be tested directly."""

    def __init__(
        self, executable: Path | None = None, cwd: Path | None = None, heartbeat_interval: float = HEARTBEAT_INTERVAL
    ):
        self.executable = executable or default_executable()
        self.cwd = (cwd or Path.cwd()).resolve()
        self.heartbeat_interval = heartbeat_interval

    async def run(self, task: str, args: list[str], ctx: Context | None = None) -> RunResult:
        async def heartbeat(elapsed: float) -> None:
            if ctx is not None:
                await ctx.report_progress(
                    progress=round(elapsed),
                    message=f"`edwh {task}` still running ({elapsed:.0f}s elapsed)",
                )

        async def once() -> RunResult:
            return await run_edwh(
                task,
                args,
                cwd=self.cwd,
                executable=self.executable,
                heartbeat=heartbeat,
                heartbeat_interval=self.heartbeat_interval,
            )

        run = await once()
        if run.error_type == "sudo_required" and await ask_sudo_retry(ctx):
            run = await once()
        return run

    async def lint(self, directory: str = ".", fix: bool = False, ctx: Context | None = None) -> responses.Response:
        args = ["--output", "ci", "--directory", directory]
        if fix:
            args.append("--fix")
        return responses.lint_response(await self.run("lint", args, ctx))

    async def fmt(self, directory: str = ".", ctx: Context | None = None) -> responses.Response:
        return responses.fmt_response(await self.run("fmt", ["--directory", directory, "--quiet"], ctx))

    async def test(self, task: str, args: list[str] | None = None, ctx: Context | None = None) -> responses.Response:
        return responses.testrun_response(await self.run(task, list(args or []), ctx))


def create_server(
    executable: Path | None = None,
    cwd: Path | None = None,
    test_task: str | t.Literal[False] | None = None,
    heartbeat_interval: float = HEARTBEAT_INTERVAL,
) -> FastMCP:
    """
    Build the server.

    `test_task=None` detects the test task via `edwh --list`; pass a task name to skip detection, or False to never
    register the `test` tool.
    """
    tools = EdwhTools(executable=executable, cwd=cwd, heartbeat_interval=heartbeat_interval)
    server = FastMCP("edwh", instructions=INSTRUCTIONS, log_level="WARNING")

    async def lint(directory: str = ".", fix: bool = False, ctx: Context | None = None) -> ToolResponse:
        return await tools.lint(directory=directory, fix=fix, ctx=ctx)

    async def fmt(directory: str = ".", ctx: Context | None = None) -> ToolResponse:
        return await tools.fmt(directory=directory, ctx=ctx)

    server.add_tool(
        lint,
        name="lint",
        title="Lint (ruff + ty)",
        description=(
            "Lint and type-check Python code with ruff and ty (`edwh lint`), inside the project virtualenv. "
            "Returns the diagnostics of failing tools. `fix=true` lets ruff apply safe autofixes." + NEVER_WORK_AROUND
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
        structured_output=True,
    )
    server.add_tool(
        fmt,
        name="fmt",
        title="Format (ruff)",
        description=(
            "Format Python code and sort imports with ruff (`edwh fmt`), inside the project virtualenv. "
            "Run it after editing code." + NEVER_WORK_AROUND
        ),
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
        structured_output=True,
    )

    if test_task is None:
        test_task = find_test_task(list_tasks(tools.executable, tools.cwd))

    if test_task:
        resolved_test_task: str = test_task

        async def test(args: list[str] | None = None, ctx: Context | None = None) -> ToolResponse:
            return await tools.test(resolved_test_task, args=args, ctx=ctx)

        server.add_tool(
            test,
            name="test",
            title="Run tests",
            description=(
                f"Run the project's tests (`edwh {resolved_test_task}`), inside the project virtualenv. "
                f"`args` are passed through to `edwh {resolved_test_task}` (see `edwh {resolved_test_task} --help`). "
                "On success only a summary is returned; on failure the last output lines. "
                "Use this instead of running pytest by hand; fix the code or the test, never skip or delete tests to "
                "make them pass." + NEVER_WORK_AROUND
            ),
            annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
            structured_output=True,
        )

    return server
