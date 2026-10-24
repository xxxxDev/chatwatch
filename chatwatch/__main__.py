"""chatwatch command line.

  chatwatch browser [--show]            start chatwatch's Chrome (--show: a window, to sign in)
  chatwatch browser stop                close it
  chatwatch serve                       watch Teams, serve the page and the API (starts Chrome)
  chatwatch open                        print the page address (with its key)
  chatwatch mcp                         MCP server over stdio (needs serve running)
  chatwatch chats                       list chats
  chatwatch read CHAT [N]               last N messages, without marking the chat read
  chatwatch send CHAT TEXT [FILE...]    send now, as the person running it
  chatwatch set CHAT [--watch on|off] [--send off|draft|auto] [--handler NAME]

CHAT is a chat's title, a part of it that matches one chat, or its id.
"""
import argparse
import json
import sys
import urllib.parse

from . import browser
from .store import HOME, api_key


def out(v):
    print(json.dumps(v, ensure_ascii=False, indent=1))


def main(argv=None):
    p = argparse.ArgumentParser(prog="chatwatch", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd")
    b = sub.add_parser("browser")
    b.add_argument("action", nargs="?", default="start", choices=["start", "stop"])
    b.add_argument("--show", action="store_true")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int)
    s.add_argument("--show", action="store_true")
    sub.add_parser("open")
    sub.add_parser("mcp")
    sub.add_parser("chats")
    r = sub.add_parser("read")
    r.add_argument("chat")
    r.add_argument("n", nargs="?", type=int, default=20)
    se = sub.add_parser("send")
    se.add_argument("chat")
    se.add_argument("text")
    se.add_argument("files", nargs="*")
    st = sub.add_parser("set")
    st.add_argument("chat")
    st.add_argument("--watch", choices=["on", "off"])
    st.add_argument("--send", choices=["off", "draft", "auto"])
    st.add_argument("--handler")
    a = p.parse_args(argv)

    if a.cmd == "mcp":
        from . import mcp
        return mcp.main()
    if a.cmd == "open":
        from . import http_api
        print("http://127.0.0.1:%d/#key=%s" % (http_api.PORT, api_key(HOME)))
        return
    if a.cmd == "browser":
        if a.action == "stop":
            print("closed" if browser.stop() else "not running (or not chatwatch's own)")
        else:
            print("started" if browser.start(show=a.show) else "already running", browser.CDP_URL)
        return
    if not a.cmd:
        p.print_help()
        return 2

    if a.cmd in ("send", "set"):
        # Through the running daemon when there is one, so only it writes
        # events and settings; otherwise directly.
        from . import mcp
        try:
            mcp.api("GET", "/api/status", timeout=5)
        except mcp.ApiError:
            pass
        else:
            return through_daemon(a, mcp)

    from .service import Service
    svc = Service()
    if a.cmd == "serve":
        import threading
        from . import http_api
        browser.start(show=a.show)
        server = http_api.serve(svc, a.port)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print("chatwatch on http://127.0.0.1:%d/ (chatwatch open for the address with its key)"
              % server.server_address[1], flush=True)
        try:
            svc.run()
        except KeyboardInterrupt:
            pass
        return
    if a.cmd == "chats":
        return out(svc.chat_list(refresh=True))
    c = svc.resolve(a.chat)
    if a.cmd == "read":
        return out({"chat": c["title"], "chat_id": c["id"], "messages": svc.teams.messages(c["id"], a.n)})
    if a.cmd == "send":
        r = svc.send(c["id"], a.text, a.files, source="person", confirm_draft=True)
        out(r)
        return 0 if r["status"] == "sent" else 1
    if a.cmd == "set":
        changes = {}
        if a.watch:
            changes["watch"] = a.watch == "on"
        if a.send:
            changes["send"] = a.send
        if a.handler is not None:
            changes["handler"] = a.handler
        return out(dict(svc.store.set_chat(c["id"], **changes), chat=c["title"]))


def through_daemon(a, mcp):
    try:
        if a.cmd == "send":
            r = mcp.api("POST", "/api/send", {"chat": a.chat, "text": a.text, "files": a.files,
                                              "source": "person", "person": True})
            out(r)
            return 0 if r["status"] == "sent" else 1
        c = [x for x in mcp.api("GET", "/api/chats")["chats"] if a.chat in (x["id"], x["title"])]
        if len(c) != 1:
            raise mcp.ApiError("give the chat's exact title or id")
        changes = {}
        if a.watch:
            changes["watch"] = a.watch == "on"
        if a.send:
            changes["send"] = a.send
        if a.handler is not None:
            changes["handler"] = a.handler
        out(dict(mcp.api("POST", "/api/chats/" + urllib.parse.quote(c[0]["id"], safe=""),
                         changes), chat=c[0]["title"]))
    except mcp.ApiError as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
