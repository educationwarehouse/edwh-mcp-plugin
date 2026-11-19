# Changelog

<!--next-version-placeholder-->

## v0.1.0 (2026-11-19)

### Feature
* **mcp:** add edwh task server for coding agents

## v0.1.0 (unreleased)

### Features
* `edwh mcp`: stdio MCP server exposing `lint`, `fmt` and (when the project has one) `test` to coding agents
* tasks run as subprocesses in the project venv, non-interactively; all output goes to a log file and only a small
  structured result (with truncated diagnostics / output tail) is returned
* progress heartbeats during long runs; `sudo_required` handled via a password-less elicitation + one retry
