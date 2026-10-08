#!/usr/bin/env python3
"""Host side of the sandbox Claude-in-Chrome bridge, with a service-worker keepalive.

socat execs one copy of this per guest connection (stdin/stdout are wired to the guest
stream). It resolves the newest live ``claude --chrome-native-host`` socket, connects,
and relays the length-prefixed JSON frames both ways — same bytes the old socat resolver
moved.

On top of that it works around the upstream Chrome bug (anthropics/claude-code #16350,
#61347): Chrome's MV3 service worker goes idle after ~30s, which closes the native
messaging port and kills the native host, so browser tools break after any idle gap.
Reading the extension's service worker shows every native message is processed by its
``onMessage`` handler (an unknown method round-trips as ``{"result":{"content":"Unknown
method: X"}}``), and processing an event resets the MV3 idle timer. So during idle gaps
this injects a benign keepalive whose method name is distinctive; the service worker
echoes that name back in its "Unknown method" reply, which lets us swallow our own
keepalive responses so the guest (the in-VM claude) never sees them.
"""

from __future__ import annotations

import getpass
import glob
import json
import os
import socket
import struct
import threading
import time

DIR = f"/tmp/claude-mcp-browser-bridge-{getpass.getuser()}"
KA_METHOD = "__claude_profile_keepalive__"
KA_MARKER = f"Unknown method: {KA_METHOD}"
KA_INTERVAL = 20.0  # < Chrome's ~30s MV3 idle timeout, with margin


def newest_sock() -> str | None:
    """Newest native-host socket, matching the resolver the socat version used."""
    socks = glob.glob(f"{DIR}/*.sock")
    socks.sort(key=os.path.getmtime, reverse=True)
    return socks[0] if socks else None


def read_frame(recv) -> bytes | None:
    """Read one 4-byte-LE-length-prefixed frame, returning the raw header+body."""
    hdr = b""
    while len(hdr) < 4:
        chunk = recv(4 - len(hdr))
        if not chunk:
            return None
        hdr += chunk
    n = struct.unpack("<I", hdr)[0]
    body = b""
    while len(body) < n:
        chunk = recv(n - len(body))
        if not chunk:
            return None
        body += chunk
    return hdr + body


def write_all(fd: int, data: bytes) -> None:
    """Write every byte of data to fd; a single os.write may write only part of it."""
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _is_keepalive_response(frame: bytes) -> bool:
    """True if a native-host frame is the echo of our own keepalive (to swallow)."""
    try:
        obj = json.loads(frame[4:])
    except (ValueError, UnicodeDecodeError):
        return False
    result = obj.get("result") if isinstance(obj, dict) else None
    return isinstance(result, dict) and result.get("content") == KA_MARKER


def main() -> None:
    sock_path = newest_sock()
    if not sock_path:
        return  # no native host; guest connection closes and claude reconnects later
    try:
        native = socket.socket(socket.AF_UNIX)
        native.connect(sock_path)
    except OSError:
        return

    stop = threading.Event()
    last_activity = [time.monotonic()]
    send_lock = threading.Lock()

    def native_send(raw: bytes) -> None:
        with send_lock:
            native.sendall(raw)

    def guest_to_native() -> None:
        try:
            while not stop.is_set():
                frame = read_frame(lambda n: os.read(0, n))
                if frame is None:
                    break
                last_activity[0] = time.monotonic()
                native_send(frame)
        except OSError:
            pass
        finally:
            stop.set()

    def native_to_guest() -> None:
        try:
            while not stop.is_set():
                frame = read_frame(native.recv)
                if frame is None:
                    break
                if _is_keepalive_response(frame):
                    continue  # swallow our own keepalive echo
                last_activity[0] = time.monotonic()
                write_all(1, frame)
        except OSError:
            pass
        finally:
            stop.set()

    def keepalive() -> None:
        body = json.dumps({"method": KA_METHOD, "id": -1, "params": {}}).encode()
        frame = struct.pack("<I", len(body)) + body
        while not stop.wait(1.0):
            if time.monotonic() - last_activity[0] >= KA_INTERVAL:
                try:
                    native_send(frame)
                except OSError:
                    break
                # count the keepalive as activity so we pace at KA_INTERVAL when idle
                last_activity[0] = time.monotonic()

    threads = [
        threading.Thread(target=guest_to_native, daemon=True),
        threading.Thread(target=native_to_guest, daemon=True),
        threading.Thread(target=keepalive, daemon=True),
    ]
    for t in threads:
        t.start()
    stop.wait()
    try:
        native.close()
    except OSError:
        pass


if __name__ == "__main__":
    main()
