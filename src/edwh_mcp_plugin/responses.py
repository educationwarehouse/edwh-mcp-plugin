"""Turn a `RunResult` into the small structured response an agent gets to see."""

import json
import re
import typing as t

from .runner import RunResult

LINT_MAX_LINES = 200
LINT_MAX_CHARS = 20_000
TAIL_LINES = 40
TAIL_MAX_CHARS = 8_000
RESULT_MAX_CHARS = 4_000

SUDO_HINT = (
    "This task needs sudo, which an agent cannot provide. Do NOT work around it "
    "(no `sudo -S`, no editing sudoers, no running the underlying commands by hand). "
    "Stop and ask the human to run `edwh sudo` in a terminal to store the sudo password in the keyring, "
    "then call this tool again."
)

VIRTUALENV_HINT = (
    "edwh is not running from the project's virtualenv. Do NOT edit pyproject.toml or install packages to work "
    "around it; ask the human to point the MCP server at `venv/bin/edwh mcp` (from the project root)."
)

Response = dict[str, t.Any]


def _truncate_lines(lines: list[str], max_lines: int, max_chars: int, *, from_end: bool) -> tuple[str, bool]:
    """Keep the first (or last) `max_lines` lines, capped at `max_chars` characters."""
    truncated = len(lines) > max_lines
    lines = lines[-max_lines:] if from_end else lines[:max_lines]
    text = "\n".join(lines)
    if len(text) > max_chars:
        truncated = True
        text = text[-max_chars:] if from_end else text[:max_chars]
    return text, truncated


def _nonblank_lines(output: str) -> list[str]:
    lines = output.rstrip().split("\n")
    # drop leading blank lines too:
    while lines and not lines[0].strip():
        lines.pop(0)
    return lines


def head(output: str, max_lines: int = LINT_MAX_LINES, max_chars: int = LINT_MAX_CHARS, *, log_path: str = "") -> str:
    text, truncated = _truncate_lines(_nonblank_lines(output), max_lines, max_chars, from_end=False)
    if truncated:
        text += f"\n[... output truncated; full output in {log_path}]"
    return text


def tail(output: str, max_lines: int = TAIL_LINES, max_chars: int = TAIL_MAX_CHARS, *, log_path: str = "") -> str:
    text, truncated = _truncate_lines(_nonblank_lines(output), max_lines, max_chars, from_end=True)
    if truncated:
        text = f"[... earlier output truncated; full output in {log_path}]\n" + text
    return text


def compact_result(value: t.Any, max_chars: int = RESULT_MAX_CHARS) -> t.Any:
    """Return the task's result as-is when small, otherwise a truncated JSON string."""
    if value is None:
        return None
    try:
        encoded = json.dumps(value, default=str)
    except (TypeError, ValueError):
        encoded = repr(value)
        value = encoded
    if len(encoded) <= max_chars:
        return value
    return encoded[:max_chars] + " [... truncated]"


def with_hint(error: dict[str, t.Any] | None) -> dict[str, t.Any] | None:
    """Make sure sudo/venv errors carry a hint that tells the agent to stop and ask the human."""
    if not error:
        return None
    error = dict(error)
    error.setdefault("message", "")
    if error.get("type") == "sudo_required":
        error["hint"] = SUDO_HINT
    elif error.get("type") == "virtualenv_required" and not error.get("hint"):
        error["hint"] = VIRTUALENV_HINT
    else:
        error.setdefault("hint", None)
    return error


def base(run: RunResult, summary: str) -> Response:
    return {
        "ok": run.ok,
        "task": " ".join([run.task, *run.args]),
        "summary": summary,
        "log_path": str(run.log_path),
        "duration_s": run.duration_s,
    }


def _failure_summary(run: RunResult) -> str:
    error = run.error or {}
    if error.get("type") == "sudo_required":
        return f"`edwh {run.task}` needs sudo; stop and ask the human (see error.hint)."
    if error.get("type") == "virtualenv_required":
        return f"`edwh {run.task}` must run from the project virtualenv; stop and ask the human (see error.hint)."
    return f"`edwh {run.task}` failed (exit code {run.exit_code})."


def _add_error(response: Response, run: RunResult) -> Response:
    if not run.ok and (error := run.error):
        response["error"] = with_hint(error)
    return response


def lint_response(run: RunResult) -> Response:
    lint = run.result if isinstance(run.result, dict) else None
    results = lint.get("results") if lint else None

    if isinstance(results, dict) and results:
        parts = [f"{name}: {'ok' if passed else 'FAILED'}" for name, passed in results.items()]
        summary = ("Lint passed. " if run.ok else "Lint failed. ") + ", ".join(parts)
    elif run.ok:
        summary = "Lint passed."
    else:
        summary = _failure_summary(run)

    response = base(run, summary)
    if lint is not None:
        response["result"] = compact_result(lint)
    if not run.ok:
        # ci mode only prints diagnostics of failing tools, and the agent needs those to fix the code.
        response["output_tail"] = head(run.output, log_path=str(run.log_path))
    return _add_error(response, run)


def fmt_response(run: RunResult) -> Response:
    summary = "Formatted." if run.ok else _failure_summary(run)
    response = base(run, summary)
    if not run.ok:
        response["output_tail"] = tail(run.output, max_lines=20, log_path=str(run.log_path))
    return _add_error(response, run)


PYTEST_SUMMARY_RE = re.compile(
    r"\b\d+ (passed|failed|error|errors|skipped|xfailed|xpassed|deselected|warnings?)\b|no tests ran"
)


def pytest_summary_line(output: str) -> str | None:
    """Find pytest's (or a similar runner's) final summary line, e.g. `==== 12 passed in 1.2s ====`."""
    for line in reversed(output.split("\n")):
        if PYTEST_SUMMARY_RE.search(line):
            return line.strip(" =")
    return None


def testrun_response(run: RunResult) -> Response:
    found = pytest_summary_line(run.output)
    if run.ok:
        summary = f"Tests passed: {found}" if found else "Tests passed."
    else:
        summary = _failure_summary(run) + (f" {found}" if found else "")

    response = base(run, summary)
    if not run.ok:
        response["output_tail"] = tail(run.output, log_path=str(run.log_path))
        if (result := compact_result(run.result)) is not None:
            response["result"] = result
    return _add_error(response, run)
