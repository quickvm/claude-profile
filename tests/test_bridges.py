"""Tests for claude_profile.bridges, the host side of the sandbox bridges."""

from __future__ import annotations

import contextlib
import socket
import threading
from collections.abc import Generator
from pathlib import Path

import pytest

from claude_profile import bridges


@pytest.fixture()
def echo_socket(tmp_path: Path) -> Generator[Path, None, None]:
    """A Unix socket that echoes whatever a client sends, like a stand-in agent."""
    path = tmp_path / "agent.sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen()

    def serve() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                while data := conn.recv(4096):
                    conn.sendall(data)

    threading.Thread(target=serve, daemon=True).start()
    yield path
    server.close()


def _connect(server: bridges.BridgeServer, first_line: str) -> socket.socket:
    client = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    client.sendall(first_line.encode() + b"\n")
    return client


def _read_all(conn: socket.socket) -> bytes:
    """Everything the bridge sends before closing. A dropped connection can arrive as a
    reset rather than a clean close when the client's bytes were never read."""
    chunks = []
    with contextlib.suppress(ConnectionResetError):
        while chunk := conn.recv(4096):
            chunks.append(chunk)
    return b"".join(chunks)


@pytest.fixture()
def agent_server(echo_socket: Path) -> Generator[bridges.BridgeServer, None, None]:
    server = bridges.BridgeServer({"ssh-0": bridges.unix_relay(echo_socket)})
    server.start()
    yield server
    server.close()


def test_bridge_relays_after_the_token(agent_server: bridges.BridgeServer) -> None:
    with _connect(agent_server, f"{agent_server.token} ssh-0") as client:
        client.sendall(b"list keys")
        client.shutdown(socket.SHUT_WR)
        assert _read_all(client) == b"list keys"


@pytest.mark.parametrize(
    "first_line", ["not-the-token ssh-0", "{token} gpg", "{token}", ""]
)
def test_bridge_drops_a_connection_without_token_and_service(
    agent_server: bridges.BridgeServer, first_line: str
) -> None:
    # Any local process, container or other VM can reach 127.0.0.1; only this launch's
    # token and a service it serves get a relay.
    line = first_line.format(token=agent_server.token)
    with _connect(agent_server, line) as client:
        client.sendall(b"list keys")
        assert _read_all(client) == b""


def test_bridge_drops_a_client_that_never_sends_the_handshake(
    agent_server: bridges.BridgeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridges, "HANDSHAKE_TIMEOUT", 0.2)
    with socket.create_connection(
        ("127.0.0.1", agent_server.port), timeout=5
    ) as client:
        assert _read_all(client) == b""


def test_bridge_tokens_differ_per_launch(echo_socket: Path) -> None:
    first = bridges.BridgeServer({"ssh-0": bridges.unix_relay(echo_socket)})
    second = bridges.BridgeServer({"ssh-0": bridges.unix_relay(echo_socket)})
    try:
        assert first.token != second.token
        assert len(first.token) >= 64
        assert first.port != second.port
    finally:
        first.close()
        second.close()


def test_bridge_stops_listening_when_closed(echo_socket: Path) -> None:
    server = bridges.BridgeServer({"ssh-0": bridges.unix_relay(echo_socket)})
    server.start()
    port = server.port
    server.close()
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=5)


def test_unix_relay_closes_when_the_agent_is_gone(tmp_path: Path) -> None:
    server = bridges.BridgeServer({"ssh-0": bridges.unix_relay(tmp_path / "gone.sock")})
    server.start()
    try:
        with _connect(server, f"{server.token} ssh-0") as client:
            assert _read_all(client) == b""
    finally:
        server.close()
