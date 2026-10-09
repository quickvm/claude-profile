"""Tests for claude_profile.bridges, the host side of the sandbox bridges."""

from __future__ import annotations

import contextlib
import json
import os
import pwd
import socket
import struct
import threading
import time
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


def _fake_tool(bin_dir: Path, name: str, record: Path, exit_code: int = 0) -> None:
    """A stand-in for a host tool that records its arguments and prints 'ran'."""
    bin_dir.mkdir(exist_ok=True)
    tool = bin_dir / name
    tool.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > {record}\necho ran\nexit {exit_code}\n'
    )
    tool.chmod(0o755)


def _request(service: str, request: str, services: dict[str, bridges.Service]) -> bytes:
    """Run one request through a bridge the way the guest shims do."""
    server = bridges.BridgeServer(services)
    server.start()
    try:
        with _connect(server, f"{server.token} {service}") as client:
            client.sendall(request.encode() + b"\n")
            return _read_all(client)
    finally:
        server.close()


@pytest.mark.parametrize(
    ("request_line", "allowed"),
    [
        ("--list-types", True),
        ("--no-newline --type image/png", True),
        ("-t text/plain", True),
        # --watch runs a command for every clipboard change: on the host.
        ("--watch touch /tmp/pwned", False),
        ("--primary", False),
        ("--type image/png --watch sh", False),
    ],
)
def test_clipboard_runs_only_read_only_wl_paste(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, request_line: str, allowed: bool
) -> None:
    record = tmp_path / "wl-paste-args"
    _fake_tool(tmp_path / "bin", "wl-paste", record)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    reply = _request("clipboard", request_line, {"clipboard": bridges.clipboard})
    assert record.exists() is allowed
    if allowed:
        assert record.read_text().split() == request_line.split()
        assert reply == b"OK\nran\n"
    else:
        assert reply == b"ERR refused\n"


def test_clipboard_reports_a_failed_wl_paste(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An empty clipboard makes wl-paste fail; the shim must fail too, not print nothing
    # and exit 0, or claude takes the empty output for an image.
    _fake_tool(tmp_path / "bin", "wl-paste", tmp_path / "args", exit_code=1)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    reply = _request("clipboard", "--type image/png", {"clipboard": bridges.clipboard})
    assert reply.startswith(b"ERR")


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://clau.de/chrome/reconnect", True),
        ("https://clau.de/chrome/permissions", True),
        ("https://clau.de/chrome/tab/123", True),
        ("https://claude.ai/chrome", True),
        ("https://claude.ai/chromebook-ad", False),
        ("https://example.com/chrome", False),
        ("https://clau.de.example.com/chrome", False),
        ("https://user@clau.de/chrome/reconnect", False),
        ("https://clau.de:8443/chrome/reconnect", False),
        ("http://clau.de/chrome/reconnect", False),
        ("javascript:alert(1)", False),
    ],
)
def test_browser_open_opens_only_claude_chrome_pages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, url: str, allowed: bool
) -> None:
    # It opens pages in the host's logged-in browser on the sandbox's say-so.
    record = tmp_path / "chrome-args"
    _fake_tool(tmp_path / "bin", "google-chrome", record)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    reply = _request("open", url, {"open": bridges.browser_open})
    assert reply == (b"OK\n" if allowed else b"NO\n")
    if allowed:
        deadline = time.monotonic() + 5  # the browser starts in the background
        while not record.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert record.read_text() == url + "\n"
    else:
        time.sleep(0.2)
        assert not record.exists()


def _frame(obj: dict) -> bytes:
    body = json.dumps(obj).encode()
    return struct.pack("<I", len(body)) + body


def _read_frame(conn: socket.socket) -> dict:
    header = b""
    while len(header) < 4:
        header += conn.recv(4 - len(header))
    size = struct.unpack("<I", header)[0]
    body = b""
    while len(body) < size:
        body += conn.recv(size - len(body))
    return json.loads(body)


@pytest.fixture()
def native_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[list[dict], None, None]:
    """A stand-in Claude-in-Chrome native host in a private bridge dir.

    It first sends the reply the extension gives to a keepalive, then answers each
    frame it receives with {"echo": frame}. Yields the frames it received.
    """
    bridge_dir = tmp_path / "claude-mcp-browser-bridge-me"
    bridge_dir.mkdir(mode=0o700)
    monkeypatch.setattr(bridges, "native_host_dir", lambda: bridge_dir)
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(bridge_dir / "4242.sock"))
    server.listen()
    received: list[dict] = []

    def serve() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            keepalive_reply = {"result": {"content": bridges.KEEPALIVE_REPLY}}
            conn.sendall(_frame(keepalive_reply))
            with contextlib.suppress(OSError, struct.error, json.JSONDecodeError):
                while True:
                    frame = _read_frame(conn)
                    received.append(frame)
                    conn.sendall(_frame({"echo": frame}))

    threading.Thread(target=serve, daemon=True).start()
    yield received
    server.close()


def test_chrome_relay_passes_frames_and_swallows_keepalive_replies(
    native_host: list[dict],
) -> None:
    server = bridges.BridgeServer({"chrome": bridges.chrome_relay})
    server.start()
    try:
        with _connect(server, f"{server.token} chrome") as client:
            client.sendall(_frame({"method": "execute_tool", "id": 1}))
            # The native host's keepalive reply comes first and must not reach claude.
            assert _read_frame(client) == {"echo": {"method": "execute_tool", "id": 1}}
    finally:
        server.close()


def test_chrome_relay_sends_a_keepalive_when_idle(
    native_host: list[dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Chrome's MV3 service worker idles out after ~30s and takes the native host with it.
    monkeypatch.setattr(bridges, "KEEPALIVE_INTERVAL", 0.2)
    server = bridges.BridgeServer({"chrome": bridges.chrome_relay})
    server.start()
    try:
        with _connect(server, f"{server.token} chrome"):
            deadline = time.monotonic() + 5
            while not native_host and time.monotonic() < deadline:
                time.sleep(0.05)
            assert native_host and native_host[0]["method"] == bridges.KEEPALIVE_METHOD
    finally:
        server.close()


@pytest.mark.parametrize(
    ("mode", "found"), [(0o700, True), (0o750, False), (0o755, False)]
)
def test_native_host_socket_only_from_a_private_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: int, found: bool
) -> None:
    # claude's own client refuses a bridge dir others can write to; the relay must too,
    # or another local user can plant a socket the VM's browser calls reach.
    bridge_dir = tmp_path / "claude-mcp-browser-bridge-me"
    bridge_dir.mkdir()
    (bridge_dir / "123.sock").touch()
    bridge_dir.chmod(mode)
    monkeypatch.setattr(bridges, "native_host_dir", lambda: bridge_dir)
    expected = bridge_dir / "123.sock" if found else None
    assert bridges.newest_native_host_socket() == expected


def test_native_host_socket_ignores_a_symlinked_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    private = tmp_path / "elsewhere"
    private.mkdir(mode=0o700)
    (private / "123.sock").touch()
    link = tmp_path / "claude-mcp-browser-bridge-me"
    link.symlink_to(private)
    monkeypatch.setattr(bridges, "native_host_dir", lambda: link)
    assert bridges.newest_native_host_socket() is None


def test_native_host_dir_uses_the_uids_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # claude names the dir after os.userInfo().username, which comes from the uid, not
    # from $USER; getpass.getuser() reads $USER first.
    monkeypatch.setenv("USER", "someone-else")
    monkeypatch.setenv("LOGNAME", "someone-else")
    name = pwd.getpwuid(os.getuid()).pw_name
    assert bridges.native_host_dir() == Path(f"/tmp/claude-mcp-browser-bridge-{name}")
