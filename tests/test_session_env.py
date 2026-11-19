import base64
import json
import socket
import struct
import tempfile
import threading
import typing as t
from pathlib import Path

import pytest

from edwh_mcp_plugin import session_env as se
from edwh_mcp_plugin.runner import subprocess_env

KEY = b"\x00\x00\x00\x07ssh-rsa-the-right-key"
OTHER_KEY = b"\x00\x00\x00\x07ssh-rsa-another-key"


def ssh_string(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


@pytest.fixture
def short_tmp() -> t.Iterator[Path]:
    # unix socket paths are limited to ~108 bytes; pytest's tmp_path can be longer.
    with tempfile.TemporaryDirectory(prefix="esock-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.fixture
def agent(short_tmp: Path) -> t.Iterator[t.Callable[..., str]]:
    """Start fake ssh-agents that answer REQUEST_IDENTITIES with the given key blobs."""
    servers: list[socket.socket] = []

    def start(name: str, *keys: bytes) -> str:
        path = str(short_tmp / name)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen()
        servers.append(server)

        def serve() -> None:
            while True:
                try:
                    conn, _ = server.accept()
                except OSError:
                    return
                with conn:
                    conn.recv(5)
                    body = bytes([se.SSH_AGENT_IDENTITIES_ANSWER]) + struct.pack(">I", len(keys))
                    body += b"".join(ssh_string(k) + ssh_string(b"comment") for k in keys)
                    conn.sendall(struct.pack(">I", len(body)) + body)

        threading.Thread(target=serve, daemon=True).start()
        return path

    yield start
    for server in servers:
        server.close()


@pytest.fixture
def keyring_config(tmp_path: Path) -> dict[str, str]:
    config = tmp_path / "config" / "ssh-agent-keyring" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"public_key": f"ssh-rsa {base64.b64encode(KEY).decode()} me@host"}))
    return {"XDG_CONFIG_HOME": str(tmp_path / "config"), "XDG_RUNTIME_DIR": str(tmp_path / "run")}


@pytest.fixture(autouse=True)
def no_real_sockets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(se.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(se.glob, "glob", lambda _: [])


def test_agent_key_blobs(agent):
    assert se.agent_key_blobs(agent("a", KEY, OTHER_KEY)) == [KEY, OTHER_KEY]
    assert se.agent_key_blobs(agent("empty")) == []
    assert se.agent_key_blobs("/nonexistent/socket") == []


def test_no_keyring_config_leaves_ssh_alone(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path / "nothing"), "XDG_RUNTIME_DIR": str(tmp_path / "run")}
    assert se.session_env(env) == {}


def test_missing_ssh_auth_sock_is_found(agent, keyring_config, monkeypatch):
    agent("empty")
    right = agent("right", OTHER_KEY, KEY)
    monkeypatch.setattr(se.glob, "glob", lambda _: [str(Path(right).with_name("empty")), right])

    assert se.session_env(keyring_config) == {"SSH_AUTH_SOCK": right}


def test_working_ssh_auth_sock_is_kept(agent, keyring_config, monkeypatch):
    current = agent("current", KEY)
    other = agent("other", KEY)
    monkeypatch.setattr(se.glob, "glob", lambda _: [other])

    assert se.session_env({**keyring_config, "SSH_AUTH_SOCK": current}) == {}


def test_agent_without_the_key_is_replaced(agent, keyring_config, monkeypatch):
    # e.g. systemd's SSH_AUTH_SOCK pointing at a gpg-agent without identities
    current = agent("gpg", OTHER_KEY)
    right = agent("right", KEY)
    monkeypatch.setattr(se.glob, "glob", lambda _: [right])

    assert se.session_env({**keyring_config, "SSH_AUTH_SOCK": current}) == {"SSH_AUTH_SOCK": right}


def test_no_agent_with_the_key(agent, keyring_config, monkeypatch):
    current = agent("gpg", OTHER_KEY)
    monkeypatch.setattr(se.glob, "glob", lambda _: [current])

    assert se.session_env({**keyring_config, "SSH_AUTH_SOCK": current}) == {}


def test_missing_dbus_address(short_tmp):
    bus = short_tmp / "bus"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(bus))
        assert se.session_env({"XDG_RUNTIME_DIR": str(short_tmp)}) == {"DBUS_SESSION_BUS_ADDRESS": f"unix:path={bus}"}
        assert se.session_env({"XDG_RUNTIME_DIR": str(short_tmp), "DBUS_SESSION_BUS_ADDRESS": "x"}) == {}


def test_subprocess_env_applies_session_env(agent, keyring_config, monkeypatch, tmp_path):
    right = agent("right", KEY)
    monkeypatch.setattr(se.glob, "glob", lambda _: [right])
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    for name, value in keyring_config.items():
        monkeypatch.setenv(name, value)

    env = subprocess_env(tmp_path / "result.json")
    assert env["SSH_AUTH_SOCK"] == right
    assert env["EDWH_INPUT_MODE"] == "defaults"
