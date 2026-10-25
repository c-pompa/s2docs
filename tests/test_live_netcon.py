import socket
import threading

from s2docs.live.netcon import NetconClient, NetconError


class FakeConsole:
    """A TCP 'CS2 console': answers each line with canned output (or echoes it)."""

    def __init__(self, answers: dict[str, str]):
        self.answers, self.received = answers, []
        self.srv = socket.create_server(("127.0.0.1", 0))
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        conn, _ = self.srv.accept()
        conn.sendall(b"welcome\n")
        buf = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode()
                self.received.append(text)
                conn.sendall((self.answers.get(text, text) + "\n").encode())


def test_sends_lines_honours_sleep_and_returns_replies():
    fake = FakeConsole({"getpos": "setpos 1.0 2.0 3.0;setang 0.0 90.0 0.0"})
    out = NetconClient("127.0.0.1", fake.port).run(["sv_cheats 1", "sleep 0", "getpos"], drain=0.2, settle=0.2)
    assert fake.received == ["sv_cheats 1", "getpos"]
    assert "setpos 1.0 2.0 3.0" in out


def test_unreachable_is_a_netcon_error():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    try:
        NetconClient("127.0.0.1", port, connect_timeout=0.5).run(["status"])
    except NetconError as exc:
        assert "unreachable" in str(exc)
    else:
        raise AssertionError("expected NetconError")
