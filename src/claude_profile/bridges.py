"""Host side of the sandbox bridges: one authenticated listener in the launcher process.

The VM reaches the host's 127.0.0.1 through pasta's --map-host-loopback, and so can any
other local process or user, a container on the host network, or another sandbox VM.
Every connection must therefore open with a line holding this launch's token and the
name of the service it wants; anything else is dropped before a byte is relayed. The
listener and its relays run as daemon threads of the launcher, so they end with it,
however it ends.
"""

from __future__ import annotations

import contextlib
import hmac
import secrets
import socket
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

# A service gets the connection right after the handshake line and owns it from there.
Service = Callable[[socket.socket], None]

# Seconds a client gets to send the handshake line before it is dropped.
HANDSHAKE_TIMEOUT = 5.0
# Longest line read from a client: the handshake, or a service's request line.
MAX_LINE = 4096


class BridgeServer:
    """Serve named bridge services on a fresh 127.0.0.1 port, behind a per-launch token.

    Binding port 0 and keeping the socket leaves no gap between picking a port and
    listening on it, which another process could otherwise slip into.
    """

    def __init__(self, services: dict[str, Service]) -> None:
        self.token = secrets.token_hex(32)
        self._services = services
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port: int = self._listener.getsockname()[1]

    def start(self) -> None:
        """Accept connections on a daemon thread until close()."""
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def close(self) -> None:
        """Stop listening. Relays already running end with their connections.

        shutdown() first: on Linux, closing a socket that another thread is blocked in
        accept() on leaves it listening until that accept returns.
        """
        with contextlib.suppress(OSError):
            self._listener.shutdown(socket.SHUT_RDWR)
        self._listener.close()

    def _accept_loop(self) -> None:
        while True:
            try:
                conn, _address = self._listener.accept()
            except OSError:
                return  # closed
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(HANDSHAKE_TIMEOUT)
            try:
                token, _, name = read_line(conn).partition(" ")
            except (OSError, ValueError):
                return
            service = self._services.get(name)
            if service is None or not hmac.compare_digest(
                token.encode(), self.token.encode()
            ):
                return
            conn.settimeout(None)
            service(conn)


def read_line(conn: socket.socket) -> str:
    """Read one newline-terminated line, without consuming anything after it.

    Byte by byte, so whatever follows the line (an agent protocol, image bytes) is left
    for the service. Raises ValueError for a line longer than MAX_LINE or cut short.
    """
    line = bytearray()
    while len(line) <= MAX_LINE:
        byte = conn.recv(1)
        if not byte:
            raise ValueError("connection closed before the end of the line")
        if byte == b"\n":
            return line.decode()
        line += byte
    raise ValueError(f"line longer than {MAX_LINE} bytes")


def unix_relay(path: Path) -> Service:
    """A service relaying the connection to the Unix socket at path (an agent)."""

    def serve(conn: socket.socket) -> None:
        with socket.socket(socket.AF_UNIX) as agent:
            try:
                agent.connect(str(path))
            except OSError:
                return
            pump(conn, agent)

    return serve


def pump(first: socket.socket, second: socket.socket) -> None:
    """Copy bytes both ways until both sides have finished sending."""
    other_way = threading.Thread(target=_copy, args=(second, first), daemon=True)
    other_way.start()
    _copy(first, second)
    other_way.join()


def _copy(source: socket.socket, destination: socket.socket) -> None:
    try:
        while data := source.recv(65536):
            destination.sendall(data)
    except OSError:
        pass  # either side went away; the shutdown below ends the other direction
    finally:
        with contextlib.suppress(OSError):
            destination.shutdown(socket.SHUT_WR)


# wl-paste arguments the clipboard service runs: listing types and reading contents.
# Anything else is refused; --watch, for one, runs a command on every clipboard change.
CLIPBOARD_FLAGS = frozenset(
    {"-l", "--list-types", "-t", "--type", "-n", "--no-newline"}
)
CLIPBOARD_TYPE_PREFIXES = ("image/", "text/")


def clipboard(conn: socket.socket) -> None:
    """Serve one read of the host clipboard through wl-paste.

    Reads a line of wl-paste arguments and replies with a status line, "OK" or
    "ERR <reason>", followed after OK by the clipboard bytes. The status lets the in-VM
    shim fail like wl-paste does, e.g. on an empty clipboard, rather than exit 0 with
    no output.
    """
    try:
        args = read_line(conn).split()
    except (OSError, ValueError):
        return
    if not all(
        arg in CLIPBOARD_FLAGS or arg.startswith(CLIPBOARD_TYPE_PREFIXES)
        for arg in args
    ):
        conn.sendall(b"ERR refused\n")
        return
    try:
        result = subprocess.run(
            ["wl-paste", *args], capture_output=True, check=False, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        conn.sendall(f"ERR {type(exc).__name__}\n".encode())
        return
    if result.returncode != 0:
        conn.sendall(f"ERR exit {result.returncode}\n".encode())
        return
    conn.sendall(b"OK\n" + result.stdout)
