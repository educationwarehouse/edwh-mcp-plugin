import json
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

FAKE_EDWH = textwrap.dedent(
    """\
    #!{python}
    # Fake `edwh`: behaviour is chosen with $FAKE_EDWH_SCENARIO.
    import json, os, sys

    scenario = os.environ.get("FAKE_EDWH_SCENARIO", "success")
    argv = sys.argv[1:]
    task = argv[0] if argv else ""
    result_file = os.environ.get("EDWH_RESULT_FILE")

    # record how we were called, so tests can check args / env / stdin:
    with open(os.environ["FAKE_EDWH_CALLS"], "a") as f:
        f.write(json.dumps({{
            "argv": argv,
            "input_mode": os.environ.get("EDWH_INPUT_MODE"),
            "no_color": os.environ.get("NO_COLOR"),
            "result_file": result_file,
            "stdin_eof": sys.stdin.read() == "" if sys.stdin else None,
            "cwd": os.getcwd(),
        }}) + "\\n")

    def write(payload):
        if result_file:
            with open(result_file, "w") as f:
                json.dump(payload, f)

    if argv[:3] == ["--list", "--list-format", "json"]:
        print("some plugin warning printed before the json")
        print(json.dumps({{
            "name": None, "help": None, "default": None,
            "tasks": [{{"name": "lint", "help": "", "aliases": []}}, {{"name": "test", "help": "", "aliases": []}}],
            "collections": (
                [] if scenario == "no-test-collection"
                else [{{"name": "test", "default": "run", "tasks": [{{"name": "run", "help": "", "aliases": []}}],
                       "collections": []}}]
            ),
        }}))
        sys.exit(0)

    if scenario == "success":
        print("\\x1b[32m⬤ all good\\x1b[0m")
        if task == "lint":
            write({{"task": "lint", "ok": True, "error": None,
                   "result": {{"mode": "ci", "results": {{"linting (ruff)": True, "type checking (ty)": True}}}}}})
        else:
            print("..........")
            print("============ 10 passed in 0.12s ============")
            write({{"task": task, "ok": True, "result": None, "error": None}})
        sys.exit(0)

    if scenario == "lint-failure":
        print("src/app.py:3:1: F401 [*] `os` imported but unused")
        print("error[unresolved-import]: Cannot resolve imported module `foo`")
        print(" --> src/app.py:4:8")
        write({{"task": "lint", "ok": False,
               "result": {{"mode": "ci", "results": {{"linting (ruff)": False, "type checking (ty)": False}}}},
               "error": None}})
        sys.exit(1)  # like real edwh: findings exit 1, but the result has no `error` (the task itself ran fine)

    if scenario == "sudo":
        print("[sudo] password for someone: ")
        write({{"task": task, "ok": False, "result": None,
               "error": {{"type": "sudo_required", "message": "sudo password needed", "hint": "run edwh sudo"}}}})
        sys.exit(1)

    if scenario == "sudo-once":
        # needs sudo the first time, succeeds after the human ran `edwh sudo`:
        marker = os.environ["FAKE_EDWH_CALLS"] + ".sudo"
        if not os.path.exists(marker):
            open(marker, "w").close()
            write({{"task": task, "ok": False, "result": None,
                   "error": {{"type": "sudo_required", "message": "sudo password needed", "hint": None}}}})
            sys.exit(1)
        write({{"task": task, "ok": True, "result": None, "error": None}})
        sys.exit(0)

    if scenario == "no-result-file":
        # a crash or killed process: no result file, just output and an exit code.
        print("Traceback (most recent call last):")
        print("RuntimeError: boom")
        sys.exit(int(os.environ.get("FAKE_EDWH_EXIT", "3")))

    if scenario == "empty-result-file":
        print("done")
        sys.exit(0)  # result file left empty

    if scenario == "noisy":
        for i in range(5000):
            print(f"\\x1b[33mnoise line {{i}}\\x1b[0m " + "x" * 40)
        print("FAILED tests/test_app.py::test_thing - assert 1 == 2", file=sys.stderr)
        sys.stderr.flush()
        print("============ 1 failed, 99 passed in 3.21s ============")
        write({{"task": task, "ok": False, "result": {{"big": "y" * 10000}},
               "error": {{"type": "failed", "message": "tests failed", "hint": None}}}})
        sys.exit(1)

    if scenario == "slow":
        import time
        time.sleep(float(os.environ.get("FAKE_EDWH_SLEEP", "0.6")))
        write({{"task": task, "ok": True, "result": None, "error": None}})
        sys.exit(0)

    sys.exit(f"unknown scenario {{scenario}}")
    """
)


@pytest.fixture
def fake_edwh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake `edwh` executable in a venv/bin-like directory."""
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    script = bin_dir / "edwh"
    script.write_text(FAKE_EDWH.format(python=sys.executable))
    script.chmod(0o755)

    monkeypatch.setenv("FAKE_EDWH_CALLS", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("FAKE_EDWH_SCENARIO", "success")
    monkeypatch.delenv("EDWH_RESULT_FILE", raising=False)
    # keep logs inside tmp_path; tempfile caches gettempdir(), so reset it.
    (tmp_path / "tmp").mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(tempfile, "tempdir", None)
    return script


@pytest.fixture
def project(tmp_path: Path) -> Path:
    directory = tmp_path / "project"
    directory.mkdir()
    return directory


@pytest.fixture
def get_calls(tmp_path: Path):
    def read() -> list[dict]:
        path = tmp_path / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    return read


@pytest.fixture
def scenario(monkeypatch: pytest.MonkeyPatch):
    return lambda name: monkeypatch.setenv("FAKE_EDWH_SCENARIO", name)
