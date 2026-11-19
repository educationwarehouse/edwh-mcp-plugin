# edwh-mcp-plugin

[`edwh`](https://github.com/educationwarehouse/edwh) plugin that gives coding agents (Claude Code, Codex, opencode,
...) a small, safe set of `edwh` tasks over [MCP](https://modelcontextprotocol.io) (stdio).
Nothing in it is specific to one client.

```bash
uv pip install edwh-mcp-plugin   # into the project's venv
# or: edwh plugin.add mcp
```

## Why

Agents that run project tooling on their own tend to:

1. **run lint outside the project venv**, so `ty` can't resolve imports, and then "fix" that by editing
   `pyproject.toml` or adding ignore comments;
2. **hit sudo prompts** they can't answer, and then try workarounds;
3. **flood their context** with docker output and hundreds of passing tests.

This plugin decides which tasks agents get, runs them in the project venv, captures all noise into a log file, and
returns only a small structured result.

## Tools

| Tool                                 | Runs                                     | Returns                                                                       |
|--------------------------------------|------------------------------------------|-------------------------------------------------------------------------------|
| `lint(directory=".", fix=false)`     | `edwh lint --output ci --directory ...`  | ruff/ty pass/fail and, on failure, the diagnostics (max 200 lines / 20k chars) |
| `fmt(directory=".")`                 | `edwh fmt --directory ... --quiet`       | ok + log path                                                                 |
| `test(args=[])`                      | `edwh test.run ...` (or `edwh test ...`) | on success a one-line summary; on failure the last 40 output lines            |

`test` is only registered when the project has a test task (`test.run` is preferred over a top-level `test` task;
detected with `edwh --list --list-format json`). `args` are passed straight to that task.

Every tool returns the same shape (as structured MCP content):

```json
{
  "ok": false,
  "task": "lint --output ci --directory .",
  "summary": "Lint failed. linting (ruff): ok, type checking (ty): FAILED",
  "result": {"mode": "ci", "results": {"linting (ruff)": true, "type checking (ty)": false}},
  "output_tail": "error[unresolved-import]: Cannot resolve imported module `foo` ...",
  "error": {"type": "failed", "message": "...", "hint": null},
  "log_path": "/tmp/edwh-mcp/3f2a9c1b0d4e/20261119-121314-123456-lint.log",
  "duration_s": 2.31
}
```

`result`, `output_tail` and `error` are only included when relevant. The full output (ANSI codes stripped) is always
in `log_path`, under `<tmp>/edwh-mcp/<hash of the project dir>/` (the 50 most recent logs per project are kept).

The tool descriptions tell agents to use these tools instead of running ruff/ty/pytest/edwh by hand, and to never edit
the lint configuration in `pyproject.toml` or add ignore comments to make a check pass.

### How tasks run

- Each call is a subprocess: `<dir of sys.executable>/edwh <task> ...`, with the server's working directory. Because
  the server runs from the project venv, so do the tasks. A subprocess also picks up changes to the project's
  `tasks.py` without restarting the server.
- The subprocess gets `EDWH_INPUT_MODE=defaults` (never prompts), `NO_COLOR=1`, stdin from `/dev/null` and
  `EDWH_RESULT_FILE`, a temp file where edwh writes the task's structured result. When there is none (e.g. an unknown
  task, or a killed process), the plugin falls back to the exit code and the end of the output.
- During long runs the server sends MCP progress notifications every ~10 seconds (when the client asks for progress),
  so a long test run doesn't look dead. If the client cancels the call, the task's whole process group is killed.
- `edwh mcp` refuses to start outside the project venv when the project has `./venv` or `./.venv`
  (`edwh.tasks.require_virtualenv`).

### Sudo

Passwords never go through MCP: the spec forbids requesting sensitive information via form elicitation, and it would
end up in the model's context. Instead, store the sudo password in your keyring once:

```bash
edwh sudo
```

MCP clients that run as a daemon (e.g. Codex' app server) often start `edwh mcp` without your session's
`SSH_AUTH_SOCK` or `DBUS_SESSION_BUS_ADDRESS`, which edwh needs to read that password back. The plugin fills these in
for the task subprocess: a missing D-Bus address from `$XDG_RUNTIME_DIR/bus`, and, when edwh's ssh-agent keyring is
configured, the first agent socket (`~/.ssh/agent.sock`, `$XDG_RUNTIME_DIR/gcr/ssh`, `/tmp/ssh-*/agent.*`, ...) that
actually holds the configured key.

When a task still reports `sudo_required`, the plugin asks **you** (not the model) through an MCP elicitation, if the
client supports it: a plain confirm, no password field. After you ran `edwh sudo` and confirmed, it retries once.
Otherwise (no elicitation support, declined, or the retry still failed) the agent gets an error whose hint tells it to
stop and ask you, not to work around it.

## Configuration

The server is started by the client as `venv/bin/edwh mcp`, **from the project root** (it runs tasks in its working
directory). If your client can't set the working directory, use absolute paths:
`/path/to/project/venv/bin/edwh mcp --cwd /path/to/project`.

Test runs can take a while; raise the client's tool-call timeout if needed.

### Claude Code

`.mcp.json` in the project root (Claude Code starts the server from the project directory):

```json
{
  "mcpServers": {
    "edwh": {
      "command": "venv/bin/edwh",
      "args": ["mcp"]
    }
  }
}
```

Or: `claude mcp add --scope project edwh -- venv/bin/edwh mcp`. For long test runs, set e.g.
`MCP_TOOL_TIMEOUT=1800000` (ms).

### Codex

`~/.codex/config.toml`, or `.codex/config.toml` in a trusted project:

```toml
[mcp_servers.edwh]
command = "venv/bin/edwh"
args = ["mcp"]
# cwd = "/path/to/project"   # when Codex isn't started from the project root
tool_timeout_sec = 1800       # test runs can be slow
```

### opencode

`opencode.json` in the project root:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "edwh": {
      "type": "local",
      "command": ["venv/bin/edwh", "mcp"],
      "enabled": true
    }
  }
}
```

### Other clients

Any client that launches stdio MCP servers works the same way: run `venv/bin/edwh mcp` from the project root.

## Development

```bash
uv venv venv && uv pip install --python venv/bin/python -e '.[dev]'
venv/bin/pytest
venv/bin/edwh lint && venv/bin/edwh fmt
```

The tests use a fake `edwh` executable, so they don't need a real project, docker or sudo.

## License

`edwh-mcp-plugin` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.
