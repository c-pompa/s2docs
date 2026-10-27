"""CS2 netcon (-netconport) client: plain TCP, one console line per command, the console streamed back."""

from __future__ import annotations

import re
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

_SLEEP = re.compile(r"^sleep\s+(\d+(?:\.\d+)?)$")


class NetconError(RuntimeError):
    pass


@dataclass
class NetconClient:
    host: str
    port: int = 2121
    connect_timeout: float = 5.0

    def _connect(self) -> socket.socket:
        try:
            return socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except OSError as exc:
            raise NetconError(f"netcon {self.host}:{self.port} unreachable: {exc}") from exc

    def run(self, lines: list[str], *, drain: float = 0.6, settle: float = 1.0) -> str:
        """Send lines in order ('sleep N' pauses), return everything the console printed meanwhile."""
        buf = bytearray()
        with self._connect() as sock:
            sock.settimeout(0.1)

            def pump(seconds: float) -> None:
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    try:
                        chunk = sock.recv(65536)
                    except TimeoutError:
                        continue
                    if not chunk:
                        return
                    buf.extend(chunk)

            pump(settle)
            for line in lines:
                m = _SLEEP.match(line)
                if m:
                    pump(float(m.group(1)))
                    continue
                sock.sendall(line.encode("utf-8") + b"\n")
                pump(drain)
        return buf.decode("utf-8", "replace")

    def poll(self, lines: list[str], probe: list[str], ready: Callable[[str], bool], *,
             timeout: float = 90.0, interval: float = 1.5, drain: float = 0.8,
             settle: float = 0.2) -> tuple[bool, str]:
        """Send ``lines`` once, then repeat ``probe`` until ``ready(text)`` or the timeout.

        One connection for the whole wait, on purpose: netcon streams console output to CONNECTED clients
        only, so a line printed between two ``run()`` calls is lost — and the line that decides the verdict
        here is printed by the engine while a map is spawning. The probe is a readback (`status`,
        `hs_ping`), never a fixed sleep, because the engine silently DROPS commands sent mid-spawn.
        """
        buf = bytearray()

        def text() -> str:
            return buf.decode("utf-8", "replace")

        with self._connect() as sock:
            sock.settimeout(0.1)

            def pump(seconds: float) -> None:
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    try:
                        chunk = sock.recv(65536)
                    except TimeoutError:
                        continue
                    if not chunk:
                        return
                    buf.extend(chunk)

            def send(line: str) -> None:
                sock.sendall(line.encode("utf-8") + b"\n")
                pump(drain)

            pump(settle)
            for line in lines:
                m = _SLEEP.match(line)
                if m:
                    pump(float(m.group(1)))
                    continue
                send(line)
            if ready(text()):
                return True, text()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                for line in probe:
                    m = _SLEEP.match(line)
                    if m:
                        pump(float(m.group(1)))
                        continue
                    send(line)
                if ready(text()):
                    return True, text()
                time.sleep(interval)
        return False, text()

    def stream(self, on_line: Callable[[str], None], stop: threading.Event) -> None:
        """Hold the connection and call on_line for every console line until stop is set (P5 listener)."""
        with self._connect() as sock:
            sock.settimeout(0.5)
            pending = b""
            while not stop.is_set():
                try:
                    chunk = sock.recv(65536)
                except TimeoutError:
                    continue
                if not chunk:
                    raise NetconError(f"netcon {self.host}:{self.port} closed")
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for raw in lines:
                    on_line(raw.decode("utf-8", "replace").rstrip("\r"))
