"""
Run `edwh <task>` as a subprocess, capture all of its noise into a log file and read back the structured result.

Nothing in here knows about MCP, so it can be tested (and reused) on its own.
"""

import asyncio
import contextlib
import hashlib
import json
import os
import re
import signal
import sys
import tempfile
import time
import typing as t
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# CSI sequences (colors, cursor movement), OSC sequences (titles, hyperlinks) and other two-byte escapes.
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")

HEARTBEAT_INTERVAL = 10.0
KEEP_LOGS = 50

Heartbeat = t.Callable[[float], t.Awaitable[None]]


def strip_ansi(text: str) -> str:
    """Remove terminal escape codes and normalize carriage-return progress bars."""
    text = ANSI_RE.sub("", text)
    # `\r\n` from a pty is just a newline; a lone `\r` (spinners, progress bars) overwrites the line.
    text = text.replace("\r\n", "\n")
    return "\n".join(line.rsplit("\r", 1)[-1] for line in text.split("\n"))


def default_executable() -> Path:
    """The `edwh` next to the running interpreter, so tasks run in the same (project) venv as the server."""
    return Path(sys.executable).with_name("edwh")


def log_dir(cwd: Path) -> Path:
    """Per-project log directory: <tmp>/edwh-mcp/<short hash of cwd>/."""
    digest = hashlib.sha256(str(cwd.resolve()).encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / "edwh-mcp" / digest


def new_log_path(cwd: Path, task: str) -> Path:
    directory = log_dir(cwd)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    safe_task = re.sub(r"[^A-Za-z0-9_.-]", "_", task)
    return directory / f"{stamp}-{safe_task}.log"


def prune_logs(directory: Path, keep: int = KEEP_LOGS) -> None:
    """Keep the log directory from growing forever; only the most recent `keep` logs survive."""
    with contextlib.suppress(OSError):
        logs = sorted(directory.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in logs[keep:]:
            old.unlink(missing_ok=True)


def read_result_file(path: Path) -> dict[str, t.Any] | None:
    """
    Read the JSON object edwh writes to EDWH_RESULT_FILE.

    Returns None when the file is missing, empty or not a JSON object (unknown task, crash, killed process).
    """
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None

    if not raw:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None

    return data if isinstance(data, dict) else None


@dataclass
class RunResult:
    task: str
    exit_code: int
    output: str
    log_path: Path
    duration_s: float
    payload: dict[str, t.Any] | None = None
    args: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        if self.exit_code != 0:
            return False
        if self.payload is not None:
            return bool(self.payload.get("ok", True))
        return True

    @property
    def result(self) -> t.Any:
        return self.payload.get("result") if self.payload else None

    @property
    def error(self) -> dict[str, t.Any] | None:
        if self.payload is not None:
            # edwh reported the outcome itself; it only sets `error` when the task could not run properly.
            # A task that ran and found problems (e.g. lint findings) is `ok: false` without an error:
            # `summary`, `result` and `output_tail` already say what's wrong.
            error = self.payload.get("error")
            return error if isinstance(error, dict) else None
        if self.ok:
            return None
        return {
            "type": "failed",
            "message": f"`edwh {self.task}` exited with code {self.exit_code}",
            "hint": None,
        }

    @property
    def error_type(self) -> str | None:
        error = self.error
        return error.get("type") if error else None


def subprocess_env(result_file: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "EDWH_INPUT_MODE": "defaults",
            "EDWH_RESULT_FILE": str(result_file),
            "NO_COLOR": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    env.pop("FORCE_COLOR", None)
    return env


def _kill(proc: asyncio.subprocess.Process) -> None:
    """Kill the whole process group: edwh starts docker/pytest/ruff children of its own."""
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


async def run_edwh(
    task: str,
    args: t.Sequence[str] = (),
    *,
    cwd: Path | None = None,
    executable: Path | None = None,
    heartbeat: Heartbeat | None = None,
    heartbeat_interval: float = HEARTBEAT_INTERVAL,
) -> RunResult:
    """
    Run `edwh <task> *args` non-interactively.

    All stdout and stderr goes to a log file; the result is read from EDWH_RESULT_FILE.
    `heartbeat(elapsed_seconds)` is awaited every `heartbeat_interval` seconds while the task runs.
    """
    cwd = (cwd or Path.cwd()).resolve()
    executable = executable or default_executable()
    log_path = new_log_path(cwd, task)

    fd, result_name = tempfile.mkstemp(prefix="edwh-mcp-result-", suffix=".json")
    os.close(fd)
    result_file = Path(result_name)

    command = [str(executable), task, *args]
    started = time.monotonic()

    try:
        with log_path.open("wb") as log:
            header = f"$ {' '.join(command)}\n# cwd: {cwd}\n\n".encode()
            log.write(header)
            log.flush()

            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd,
                env=subprocess_env(result_file),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )

            try:
                while True:
                    try:
                        await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=heartbeat_interval)
                        break
                    except TimeoutError:
                        if heartbeat:
                            with contextlib.suppress(Exception):
                                await heartbeat(time.monotonic() - started)
            except BaseException:
                # cancelled by the client (or anything else): don't leave a test run behind.
                _kill(proc)
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(proc.wait(), timeout=5)
                raise

        duration = time.monotonic() - started
        # skip the header we wrote ourselves:
        output = strip_ansi(log_path.read_bytes()[len(header) :].decode("utf-8", errors="replace"))
        # agents may grep the log, so keep it free of escape codes too:
        log_path.write_bytes(header + output.encode())
        payload = read_result_file(result_file)
    finally:
        result_file.unlink(missing_ok=True)
        prune_logs(log_path.parent)

    return RunResult(
        task=task,
        args=list(args),
        exit_code=proc.returncode if proc.returncode is not None else -1,
        output=output,
        log_path=log_path,
        duration_s=round(duration, 2),
        payload=payload,
    )


def list_tasks(executable: Path | None = None, cwd: Path | None = None, timeout: float = 60) -> dict[str, t.Any] | None:
    """`edwh --list --list-format json`, tolerant of noise printed before the JSON."""
    import subprocess

    executable = executable or default_executable()
    try:
        proc = subprocess.run(
            [str(executable), "--list", "--list-format", "json"],
            cwd=cwd,
            env={**os.environ, "EDWH_INPUT_MODE": "defaults", "NO_COLOR": "1"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    text = proc.stdout
    start = text.find("{")
    if start < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def find_test_task(listing: dict[str, t.Any] | None) -> str | None:
    """Prefer `test.run`, fall back to a top-level `test` task."""
    if not listing:
        return None

    def names(items: t.Iterable[t.Any] | None) -> set[str]:
        found: set[str] = set()
        for item in items or ():
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                found.add(item["name"])
                found.update(a for a in item.get("aliases") or () if isinstance(a, str))
        return found

    for collection in listing.get("collections") or ():
        if (
            isinstance(collection, dict)
            and collection.get("name") == "test"
            and "run" in names(collection.get("tasks"))
        ):
            return "test.run"

    if "test" in names(listing.get("tasks")):
        return "test"

    return None
