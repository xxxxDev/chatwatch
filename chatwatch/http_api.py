"""Local HTTP API and the web page, on 127.0.0.1 only.

Every /api call needs the header X-Chatwatch-Key with the key in
CHATWATCH_HOME/api-key. Requests with a foreign Host or Origin are refused
(stops DNS rebinding and cross-site posts). The page itself is open; it takes
the key from the address (#key=...), which `chatwatch open` prints.

  GET  /api/status
  GET  /api/chats[?refresh=1]
  POST /api/chats/<id>              {watch?, send?, handler?}
  GET  /api/messages?chat=&n=
  GET  /api/events?after=&wait=&types=message,sent
  POST /api/send                    {chat, text, files?}
  POST /api/attachments             {chat, mids}
  GET  /api/drafts
  POST /api/drafts/<id>/send        {text?, checked?}
  POST /api/drafts/<id>/discard
  POST /api/pause                   {paused}
  GET  /api/screen                  live view: {data (jpeg, base64), width, height, host}
  POST /api/screen/input            {type: click|wheel|text|key|reload, x?, y?, dx?, dy?, text?, key?}
"""
import hmac
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .service import save_attachments
from .store import api_key

PORT = int(os.environ.get("CHATWATCH_PORT", "8766"))
WEB = Path(__file__).resolve().parent / "web"


def make_handler(service, port):
    key = api_key(service.store.home)
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://{h}" for h in hosts}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

        def _allowed(self):
            origin = self.headers.get("Origin")
            return self.headers.get("Host") in hosts and (origin is None or origin in origins)

        def _body(self):
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                raise ValueError("send JSON")
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _route(self, method):
            if not self._allowed():
                return self._send(403, {"error": "local page only"})
            u = urllib.parse.urlsplit(self.path)
            q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
            parts = [urllib.parse.unquote(p) for p in u.path.strip("/").split("/")]
            if method == "GET" and u.path in ("/", "/index.html"):
                return self._send(200, (WEB / "index.html").read_bytes(), "text/html")
            if parts[0] != "api":
                return self._send(404, {"error": "not found"})
            given = (self.headers.get("X-Chatwatch-Key") or "").encode("utf-8", "replace")
            if not hmac.compare_digest(given, key.encode()):
                return self._send(401, {"error": "missing or wrong X-Chatwatch-Key"})
            try:
                body = self._body() if method == "POST" else {}
                return self._send(200, self._api(method, parts[1:], q, body))
            except (LookupError, KeyError) as e:
                return self._send(404, {"error": str(e).strip("'")})
            except (ValueError, FileNotFoundError, PermissionError) as e:
                return self._send(400, {"error": str(e)})
            except TimeoutError as e:
                return self._send(409, {"error": str(e)})
            except Exception as e:
                return self._send(502, {"error": str(e)[:300]})

        def _api(self, method, p, q, body):
            s = service
            if method == "GET" and p == ["status"]:
                cfg = s.store.config()
                return {"error": s.error, "last_poll": s.last_poll, "paused": bool(cfg.get("paused")),
                        "drafts": len(s.store.drafts()), "seq": s.store.seq,
                        "handlers": sorted(cfg.get("handlers", {}))}
            if method == "GET" and p == ["chats"]:
                return {"chats": s.chat_list(refresh=q.get("refresh") == "1")}
            if method == "POST" and len(p) == 2 and p[0] == "chats":
                changes = {k: body[k] for k in ("watch", "send", "handler") if k in body}
                return s.store.set_chat(p[1], **changes)
            if method == "GET" and p == ["messages"]:
                c = s.resolve(q.get("chat") or "")
                return {"chat": c["title"], "chat_id": c["id"],
                        "messages": s.teams.messages(c["id"], min(int(q.get("n") or 20), 50))}
            if method == "GET" and p == ["events"]:
                types = set(filter(None, (q.get("types") or "").split(",")))
                wait = min(float(q.get("wait") or 0), 60)
                events, seq = s.store.events_after(int(q.get("after") or 0), types, wait)
                return {"events": events, "seq": seq}
            if method == "POST" and p == ["send"]:
                # person: the command line's send, which sends now whatever the mode.
                # The MCP tools never set it; any holder of the key could.
                return s.send(body.get("chat") or "", body.get("text"), body.get("files"),
                              source=body.get("source") or "agent", confirm_draft=bool(body.get("person")))
            if method == "POST" and p == ["attachments"]:
                c = s.resolve(body.get("chat") or "")
                mids = set(map(str, body.get("mids") or []))
                msgs = [m for m in s.teams.messages(c["id"], 50) if m["mid"] in mids]
                return {"paths": save_attachments(s.teams, msgs)}
            if method == "GET" and p == ["drafts"]:
                return {"drafts": sorted(s.store.drafts().values(), key=lambda d: d["created"])}
            if method == "POST" and len(p) == 3 and p[0] == "drafts" and p[2] == "send":
                return s.send_draft(p[1], body.get("text"), bool(body.get("checked")))
            if method == "POST" and len(p) == 3 and p[0] == "drafts" and p[2] == "discard":
                if not s.store.remove_draft(p[1]):
                    raise KeyError("no draft " + p[1])
                return {"ok": True}
            if method == "GET" and p == ["screen"]:
                return s.teams.screen()
            if method == "POST" and p == ["screen", "input"]:
                return s.teams.operate(body)
            if method == "POST" and p == ["pause"]:
                return {"paused": s.store.set_paused(body.get("paused"))}
            raise LookupError("no such API")

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

    return Handler


def serve(service, port=None):
    """The server on 127.0.0.1 (port 0: any free port); call serve_forever()."""
    server = ThreadingHTTPServer(("127.0.0.1", PORT if port is None else port), BaseHTTPRequestHandler)
    server.RequestHandlerClass = make_handler(service, server.server_address[1])
    server.daemon_threads = True
    return server
