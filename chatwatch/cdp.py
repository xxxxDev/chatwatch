"""A small Chrome DevTools Protocol client (standard library only).

WebSocket is a minimal RFC 6455 client; CDP keeps one connection with a reader
thread; Tab adds the few page actions the Teams code needs (evaluate, click,
key, bring to front, file chooser).
"""
import base64
import json
import os
import socket
import struct
import threading
import urllib.parse
import urllib.request

CALL_TIMEOUT = 30.0


class WebSocket:
    """Minimal RFC 6455 client: text frames out, text/binary frames in."""

    def __init__(self, url, timeout=10):
        u = urllib.parse.urlsplit(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (("?" + u.query) if u.query else "")
        # No Origin header: Chrome only enforces --remote-allow-origins when one is sent.
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise ConnectionError("WebSocket handshake closed")
            head += chunk
        status, _, rest = head.partition(b"\r\n\r\n")
        if b" 101 " not in status.split(b"\r\n", 1)[0]:
            raise ConnectionError("WebSocket handshake refused: " + status.split(b"\r\n", 1)[0].decode(errors="replace"))
        self.buf = rest
        self.sock.settimeout(None)
        self.wlock = threading.Lock()

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise ConnectionError("WebSocket closed")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def _send_frame(self, opcode, payload):
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self.wlock:
            self.sock.sendall(head + mask + masked)

    def send(self, text):
        self._send_frame(0x1, text.encode())

    def recv(self):
        """Next complete message as str; answers pings; raises ConnectionError on close."""
        parts = []
        while True:
            b1, b2 = self._read(2)
            opcode, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise ConnectionError("WebSocket closed by peer")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            parts.append(payload)
            if b1 & 0x80:
                return b"".join(parts).decode(errors="replace")

    def close(self):
        for f in (lambda: self._send_frame(0x8, b""), self.sock.close):
            try:
                f()
            except OSError:
                pass


class CDP:
    """One DevTools connection with a reader thread; call() is thread-safe."""

    def __init__(self, ws_url):
        self.ws = WebSocket(ws_url)
        self.next_id = 0
        self.pending = {}
        self.events = []
        self.lock = threading.Lock()
        self.closed = False
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        try:
            while True:
                msg = json.loads(self.ws.recv())
                if "id" in msg:
                    slot = self.pending.pop(msg["id"], None)
                    if slot:
                        slot[1].append(msg)
                        slot[0].set()
                else:
                    with self.lock:
                        self.events.append(msg)
                        del self.events[:-200]
        except (OSError, ConnectionError, ValueError):
            pass
        finally:
            self.closed = True
            for ev, _box in list(self.pending.values()):
                ev.set()

    def call(self, method, params=None, timeout=CALL_TIMEOUT):
        if self.closed:
            raise ConnectionError("DevTools connection closed")
        with self.lock:
            self.next_id += 1
            mid = self.next_id
            ev, box = threading.Event(), []
            self.pending[mid] = (ev, box)
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        if not ev.wait(timeout) or not box:
            self.pending.pop(mid, None)
            raise TimeoutError(f"Chrome did not answer in time ({method})")
        if "error" in box[0]:
            raise RuntimeError(f"{method}: {box[0]['error'].get('message')}")
        return box[0].get("result", {})

    def drain_events(self):
        with self.lock:
            out, self.events = self.events, []
        return out

    def close(self):
        self.closed = True
        self.ws.close()


def http_json(base, path, method="GET"):
    req = urllib.request.Request(base + path, method=method)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


class Tab:
    """One page target, with the actions the Teams code uses."""

    def __init__(self, target):
        self.target = target
        self.cdp = CDP(target["webSocketDebuggerUrl"])

    def js(self, expression, timeout=CALL_TIMEOUT):
        """Evaluate in the page, awaiting a promise; return the value as JSON data."""
        r = self.cdp.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True,
                                               "returnByValue": True}, timeout=timeout)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            msg = (d.get("exception") or {}).get("description") or d.get("text") or "page error"
            raise RuntimeError(msg.splitlines()[0][:300])
        return (r.get("result") or {}).get("value")

    def call(self, js_function, *args, timeout=CALL_TIMEOUT):
        """Call a page function written as a JS expression with JSON arguments."""
        return self.js("(%s)(%s)" % (js_function, ",".join(json.dumps(a) for a in args)), timeout=timeout)

    def click(self, x, y):
        for kind in ("mousePressed", "mouseReleased"):
            self.cdp.call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y,
                                                       "button": "left", "clickCount": 1})

    KEYS = {"Backspace": 8, "Tab": 9, "Enter": 13, "Escape": 27, "PageUp": 33, "PageDown": 34,
            "End": 35, "Home": 36, "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40,
            "Delete": 46}

    def key(self, name, modifiers=0):
        """Press a named key (see KEYS); modifiers: 1 Alt, 2 Ctrl, 4 Meta, 8 Shift."""
        if name not in self.KEYS:
            raise ValueError("unknown key " + name)
        p = {"key": name, "code": name, "windowsVirtualKeyCode": self.KEYS[name], "modifiers": modifiers}
        down = dict(p, type="keyDown")
        if name == "Enter":
            down["text"] = "\r"
        self.cdp.call("Input.dispatchKeyEvent", down)
        self.cdp.call("Input.dispatchKeyEvent", dict(p, type="keyUp"))

    def text(self, text):
        self.cdp.call("Input.insertText", {"text": text})

    def wheel(self, x, y, dx, dy):
        self.cdp.call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y,
                                                   "deltaX": dx, "deltaY": dy})

    def screenshot(self, quality=70):
        """(jpeg bytes base64, css width, css height) of what the tab shows."""
        m = self.cdp.call("Page.getLayoutMetrics")
        vp = m.get("cssVisualViewport") or m.get("visualViewport") or {}
        r = self.cdp.call("Page.captureScreenshot", {"format": "jpeg", "quality": quality}, timeout=10)
        return r["data"], vp.get("clientWidth"), vp.get("clientHeight")

    def bring_to_front(self):
        # Headless Chrome paints only the front tab of a window.
        self.cdp.call("Page.bringToFront")

    def close(self):
        self.cdp.close()
