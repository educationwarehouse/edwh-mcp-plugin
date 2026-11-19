"""
Repair the desktop-session environment for the edwh subprocess.

MCP clients (especially daemons such as Codex' app server) often start `edwh mcp` without the user's session
variables. edwh then can't read the sudo password that `edwh sudo` stored in the keyring:
- the ssh-agent keyring needs `SSH_AUTH_SOCK` pointing at an agent that holds the configured key;
- the Secret Service keyring needs `DBUS_SESSION_BUS_ADDRESS`.
edwh swallows those keyring errors and reports `sudo_required`, so fill in what's missing before running a task.
"""

import base64
import contextlib
import glob
import json
import os
import socket
import struct
import typing as t
from pathlib import Path

AGENT_TIMEOUT = 2.0

SSH_AGENTC_REQUEST_IDENTITIES = 11
SSH_AGENT_IDENTITIES_ANSWER = 12


def runtime_dir(env: t.Mapping[str, str]) -> Path:
    return Path(env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


def ssh_agent_keyring_key(env: t.Mapping[str, str]) -> bytes | None:
    """The public key blob edwh's ssh-agent keyring encrypts with, or None when that keyring isn't configured."""
    config_root = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    try:
        config = json.loads((config_root / "ssh-agent-keyring" / "config.json").read_text())
        return base64.b64decode(config["public_key"].split()[1])
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("agent closed the connection")
        data += chunk
    return data


def agent_key_blobs(path: str, timeout: float = AGENT_TIMEOUT) -> list[bytes]:
    """Public key blobs an ssh-agent holds (empty when the socket is missing, dead or not an agent)."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            sock.sendall(struct.pack(">IB", 1, SSH_AGENTC_REQUEST_IDENTITIES))
            (length,) = struct.unpack(">I", _recv_exact(sock, 4))
            body = _recv_exact(sock, length)
    except (OSError, ValueError, struct.error):
        return []

    if not body or body[0] != SSH_AGENT_IDENTITIES_ANSWER:
        return []

    blobs: list[bytes] = []
    with contextlib.suppress(struct.error):
        (count,) = struct.unpack_from(">I", body, 1)
        offset = 5
        for _ in range(count):
            (blob_len,) = struct.unpack_from(">I", body, offset)
            offset += 4
            blobs.append(body[offset : offset + blob_len])
            offset += blob_len
            (comment_len,) = struct.unpack_from(">I", body, offset)
            offset += 4 + comment_len
    return blobs


def candidate_agent_sockets(env: t.Mapping[str, str]) -> list[str]:
    """Places where an ssh-agent socket usually lives, most specific first."""
    run = runtime_dir(env)
    candidates = [
        env.get("SSH_AUTH_SOCK", ""),
        str(Path.home() / ".ssh" / "agent.sock"),
        str(run / "gcr" / "ssh"),
        str(run / "keyring" / "ssh"),
        str(run / "ssh-agent.socket"),
        str(run / "openssh_agent"),
        str(run / "gnupg" / "S.gpg-agent.ssh"),
        *sorted(glob.glob("/tmp/ssh-*/agent.*")),
    ]
    return [c for c in dict.fromkeys(candidates) if c]


def find_ssh_agent(env: t.Mapping[str, str], key: bytes) -> str | None:
    """The first agent socket that holds `key`."""
    for path in candidate_agent_sockets(env):
        if key in agent_key_blobs(path):
            return path
    return None


def session_env(env: t.Mapping[str, str]) -> dict[str, str]:
    """The variables to add to (or override in) `env` so edwh can reach the user's keyring."""
    updates: dict[str, str] = {}

    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        bus = runtime_dir(env) / "bus"
        if bus.is_socket():
            updates["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"

    key = ssh_agent_keyring_key(env)
    if key is not None:
        current = env.get("SSH_AUTH_SOCK", "")
        if not (current and key in agent_key_blobs(current)):
            agent = find_ssh_agent(env, key)
            if agent and agent != current:
                updates["SSH_AUTH_SOCK"] = agent

    return updates
