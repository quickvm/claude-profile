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
import json
import os
import pwd
import secrets
import shutil
import socket
import stat
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

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


# Hosts whose /chrome pages the sandbox may open: claude's Claude-in-Chrome connect,
# reconnect and permission pages.
CHROME_PAGE_HOSTS = frozenset({"clau.de", "claude.ai"})


def chrome_page_allowed(url: str) -> bool:
    """True for https://clau.de or https://claude.ai URLs at /chrome or below it.

    The sandbox decides what opens in the host's logged-in browser, so the URL is parsed
    rather than prefix-matched: no other host, scheme, port or credentials.
    """
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username or parts.password or port is not None:
        return False
    if parts.hostname not in CHROME_PAGE_HOSTS:
        return False
    return parts.path == "/chrome" or parts.path.startswith("/chrome/")


def browser_open(conn: socket.socket) -> None:
    """Open a Claude-in-Chrome page in the host's Chrome; reply "OK", or "NO" if refused."""
    try:
        url = read_line(conn)
    except (OSError, ValueError):
        return
    chrome = _host_chrome()
    if chrome is None or not chrome_page_allowed(url):
        conn.sendall(b"NO\n")
        return
    subprocess.Popen(
        [chrome, url],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    conn.sendall(b"OK\n")


def _host_chrome() -> str | None:
    for name in ("google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found is not None:
            return found
    default = "/opt/google/chrome/chrome"
    return default if os.access(default, os.X_OK) else None


# The Claude-in-Chrome keepalive. Chrome's MV3 service worker goes idle after ~30s
# without events, which closes the native-messaging port and kills the native host
# (upstream anthropics/claude-code #16350, #61347). Every native message reaches the
# service worker's onMessage handler, and an unknown method comes back as
# {"result": {"content": "Unknown method: <name>"}}, so a message with a distinctive
# method name resets the idle timer and its reply can be recognised and dropped.
KEEPALIVE_METHOD = "__claude_profile_keepalive__"
KEEPALIVE_REPLY = f"Unknown method: {KEEPALIVE_METHOD}"
# Seconds without traffic before a keepalive goes out; under the ~30s idle timeout.
KEEPALIVE_INTERVAL = 20.0


def native_host_dir() -> Path:
    """Where Claude-in-Chrome native hosts bind their sockets.

    Named after the uid's user name, as claude's os.userInfo().username is, rather than
    $USER, which getpass.getuser() would read first.
    """
    return Path(f"/tmp/claude-mcp-browser-bridge-{pwd.getpwuid(os.getuid()).pw_name}")


def newest_native_host_socket() -> Path | None:
    """The newest native-host socket, or None unless the dir is private to this user.

    claude's own client refuses a bridge dir that is not mode 0700 and owned by the
    user. Without the same check, another local user who created the dir first could
    plant a socket and receive the browser calls relayed from the sandbox. The newest
    socket follows Chrome across native-host restarts, which change its pid.
    """
    directory = native_host_dir()
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        return None
    sockets = sorted(directory.glob("*.sock"), key=lambda p: p.stat().st_mtime)
    return sockets[-1] if sockets else None


def chrome_relay(conn: socket.socket) -> None:
    """Relay the in-VM claude's native-messaging frames to the host's native host."""
    path = newest_native_host_socket()
    if path is None:
        return  # no native host yet; claude reconnects later
    with socket.socket(socket.AF_UNIX) as native:
        try:
            native.connect(str(path))
        except OSError:
            return
        _ChromeRelay(conn, native).run()


class _ChromeRelay:
    """Two-way frame relay that keeps the extension's service worker awake."""

    def __init__(self, guest: socket.socket, native: socket.socket) -> None:
        self._guest = guest
        self._native = native
        self._done = threading.Event()
        self._last_traffic = time.monotonic()
        self._native_lock = threading.Lock()

    def run(self) -> None:
        for target in (self._guest_to_native, self._native_to_guest, self._keepalive):
            threading.Thread(target=target, daemon=True).start()
        self._done.wait()

    def _send_native(self, frame: bytes) -> None:
        with self._native_lock:
            self._native.sendall(frame)

    def _guest_to_native(self) -> None:
        with contextlib.suppress(OSError):
            while frame := read_frame(self._guest):
                self._last_traffic = time.monotonic()
                self._send_native(frame)
        self._done.set()

    def _native_to_guest(self) -> None:
        with contextlib.suppress(OSError):
            while frame := read_frame(self._native):
                if _is_keepalive_reply(frame):
                    continue
                self._last_traffic = time.monotonic()
                self._guest.sendall(frame)
        self._done.set()

    def _keepalive(self) -> None:
        body = json.dumps({"method": KEEPALIVE_METHOD, "id": -1, "params": {}}).encode()
        frame = struct.pack("<I", len(body)) + body
        while not self._done.wait(min(1.0, KEEPALIVE_INTERVAL)):
            if time.monotonic() - self._last_traffic < KEEPALIVE_INTERVAL:
                continue
            try:
                self._send_native(frame)
            except OSError:
                self._done.set()
                return
            self._last_traffic = time.monotonic()


def read_frame(conn: socket.socket) -> bytes:
    """One native-messaging frame (4-byte little-endian length, then the body), with its
    header; b"" when the connection closes first."""
    header = _read_exactly(conn, 4)
    if header is None:
        return b""
    body = _read_exactly(conn, struct.unpack("<I", header)[0])
    return header + body if body is not None else b""


def _read_exactly(conn: socket.socket, size: int) -> bytes | None:
    """size bytes from conn, or None if it closes first."""
    data = b""
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def _is_keepalive_reply(frame: bytes) -> bool:
    try:
        message = json.loads(frame[4:])
    except (ValueError, UnicodeDecodeError):
        return False
    result = message.get("result") if isinstance(message, dict) else None
    return isinstance(result, dict) and result.get("content") == KEEPALIVE_REPLY
