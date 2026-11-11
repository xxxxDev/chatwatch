"""MCP server over stdio: Teams tools for any MCP client (Claude Code, Codex, ...).

It is a thin client of the running daemon (`chatwatch serve`), which owns the
browser. Sending follows each chat's send mode, so an agent cannot send to a
chat a person left on draft or off; it gets back {status: "draft"} or
{status: "refused"}. Agents cannot send drafts or change settings: those are
on the web page, for a person.

Register it, e.g. for Claude Code:
  claude mcp add teams -- chatwatch mcp
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .store import HOME, api_key

URL = os.environ.get("CHATWATCH_URL") or "http://127.0.0.1:%s" % os.environ.get("CHATWATCH_PORT", "8766")
PROTOCOL = "2025-06-18"

TOOLS = [
    {"name": "teams_list_chats",
     "description": "List the Teams chats with their title, id, last message preview, unread flag and settings "
                    "(watch, send mode off|draft|auto, handler). refresh=true reads the page again.",
     "inputSchema": {"type": "object", "properties": {"refresh": {"type": "boolean"}}}},
    {"name": "teams_read_messages",
     "description": "Read the last n messages of a chat (title, title fragment or id) without marking it read.",
     "inputSchema": {"type": "object", "required": ["chat"],
                     "properties": {"chat": {"type": "string"}, "n": {"type": "integer", "minimum": 1, "maximum": 50}}}},
    {"name": "teams_wait_for_messages",
     "description": "New incoming messages in watched chats after event number after_seq, waiting up to "
                    "wait_seconds (max 60) for one. Without after_seq, only messages from now on. Returns "
                    "events and seq: pass seq as after_seq next time. With chat, other chats' messages are "
                    "skipped and the answer may be empty before wait_seconds.",
     "inputSchema": {"type": "object",
                     "properties": {"after_seq": {"type": "integer"}, "wait_seconds": {"type": "number"},
                                    "chat": {"type": "string", "description": "only this chat (title or id)"}}}},
    {"name": "teams_send_message",
     "description": "Send text, and optionally local files (absolute paths), to a chat. The chat's send mode "
                    "decides: auto sends now (status sent), draft saves it for a person to approve (status "
                    "draft), off refuses. not_sent: nothing went out, you may try again. maybe_sent: do not "
                    "resend; ask a person to check the chat. Sending opens the chat, which marks it read.",
     "inputSchema": {"type": "object", "required": ["chat", "text"],
                     "properties": {"chat": {"type": "string"}, "text": {"type": "string"},
                                    "files": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "teams_get_attachments",
     "description": "Download the pictures and files of the given messages (mid values) of a chat into a new "
                    "local folder; returns the paths.",
     "inputSchema": {"type": "object", "required": ["chat", "message_ids"],
                     "properties": {"chat": {"type": "string"},
                                    "message_ids": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "teams_list_drafts",
     "description": "Replies waiting for a person to send or discard them on the chatwatch page.",
     "inputSchema": {"type": "object", "properties": {}}},
]


class ApiError(Exception):
    pass


def api(method, path, body=None, timeout=300):
    req = urllib.request.Request(URL + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"X-Chatwatch-Key": api_key(HOME), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("error")
        except ValueError:
            msg = None
        raise ApiError(msg or "HTTP %d" % e.code)
    except OSError as e:
        raise ApiError("chatwatch is not running at %s (start it with: chatwatch serve): %s" % (URL, e))


def call_tool(name, a):
    if name == "teams_list_chats":
        return api("GET", "/api/chats" + ("?refresh=1" if a.get("refresh") else ""))
    if name == "teams_read_messages":
        return api("GET", "/api/messages?" + urllib.parse.urlencode({"chat": a["chat"], "n": a.get("n", 20)}))
    if name == "teams_wait_for_messages":
        after = a.get("after_seq")
        if after is None:
            after = api("GET", "/api/status")["seq"]
        wait = max(0, min(float(a.get("wait_seconds", 30)), 60))
        r = api("GET", "/api/events?" + urllib.parse.urlencode({"after": after, "wait": wait, "types": "message"}),
                timeout=wait + 30)
        if a.get("chat"):  # other chats' messages are skipped, so this can come back empty early
            key = a["chat"].lower()
            r["events"] = [e for e in r["events"] if key == e["chat_id"].lower() or key in e["chat"].lower()]
        return r
    if name == "teams_send_message":
        return api("POST", "/api/send", {"chat": a["chat"], "text": a["text"], "files": a.get("files") or [],
                                         "source": "mcp"})
    if name == "teams_get_attachments":
        return api("POST", "/api/attachments", {"chat": a["chat"], "mids": a["message_ids"]})
    if name == "teams_list_drafts":
        return api("GET", "/api/drafts")
    raise ApiError("unknown tool " + name)


def handle(msg):
    """One JSON-RPC message in; the response (or None for a notification)."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL,
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "chatwatch-teams", "version": __version__}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            out = call_tool(params.get("name"), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}]}
        except (ApiError, KeyError, ValueError) as e:
            result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            out = handle(msg)
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            sys.stdout.flush()
