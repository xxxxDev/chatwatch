"""The running watcher: polls Teams, records new messages, pushes them to a
chat's handler, and sends replies under each chat's send mode.

Send modes (per chat, config.json):
  off    nothing is sent from an agent or handler; refused
  draft  the text waits in drafts until a person sends or discards it (default)
  auto   sent at once

Two ways for an agent to work with a chat:
  pull   through MCP (mcp.py): read new messages, then send
  push   a chat's handler gets each batch of new messages and returns a reply
"""
import base64
import json
import os
import queue
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from .store import Store
from .teams import MaybeSent, NotSent, Teams

POLL_SECONDS = float(os.environ.get("CHATWATCH_POLL", "4"))
AUTO_RETRY_SECONDS = (20, 60, 180)   # a handler reply in an auto chat that did not go
HANDLER_TIMEOUT = 600
MODE_CHANGED = "SEND_MODE_CHANGED"


class Service:
    def __init__(self, store=None, teams=None):
        self.store = store or Store()
        self.teams = teams or Teams(self.store.home)
        self.chats = []          # last chat list read from the page
        self.error = None
        self.last_poll = None
        self.jobs = queue.Queue()
        self.stop = threading.Event()

    # ---------- chats ----------

    def chat_list(self, refresh=False):
        if refresh or not self.chats:
            self.chats = self.teams.chats() or self.chats
        return [dict(c, **self.store.settings(c["id"])) for c in self.chats]

    def resolve(self, key):
        """A chat by id; else by a title, or part of one, that only one chat has.
        Ids win over titles: anyone in a group chat can rename it."""
        key = (key or "").strip()
        if not key:
            raise LookupError("no chat named")
        for refresh in (False, True):
            chats = self.chat_list(refresh)
            for c in chats:
                if c["id"] == key:
                    return c
            for hits in ([c for c in chats if c["title"] == key],
                         [c for c in chats if key.lower() in c["title"].lower()]):
                if len(hits) == 1:
                    return hits[0]
                if hits:
                    raise LookupError("several chats match %r; use the chat id: %s"
                                      % (key, ", ".join("%s (%s)" % (c["title"], c["id"]) for c in hits)))
        raise LookupError("no chat matches %r" % key)

    # ---------- watching ----------

    def poll_once(self):
        chats = self.teams.chats()
        # The list can read empty for a moment while Teams redraws.
        self.chats, self.last_poll = (chats or self.chats), time.time()
        titles = {c["id"]: c for c in self.chats}
        convs = self.teams.conversations()
        state = self.store.state()
        known = state["chats"]
        first_run = not any("last_id" in v for v in known.values())
        failed = []
        for c in convs:
            entry = known.setdefault(c["id"], {})
            prev_id = entry.get("last_id")
            if first_run or prev_id is None:
                entry.update(last_id=c["last_id"], last_ms=c["last_ms"])
                continue
            if prev_id == c["last_id"]:
                continue
            # Each chat counts as seen only once relayed, and is saved at once:
            # one chat failing must not make another's batch come twice.
            try:
                self.relay(c["id"], titles.get(c["id"]), entry)
            except Exception as e:
                failed.append("%s: %s" % (titles.get(c["id"], {}).get("title", c["id"]), e))
                continue
            entry["last_id"] = c["last_id"]
            self.store.save_state(state)
        self.store.save_state(state)
        if failed:
            raise RuntimeError("could not read " + "; ".join(failed))

    def relay(self, chat_id, chat, entry):
        msgs = self.teams.messages(chat_id, 20)
        s = self.store.settings(chat_id)
        last_ms = entry.get("last_ms") or 0
        new = [m for m in msgs if not m["mine"] and m["ms"] > last_ms]
        if msgs:
            entry["last_ms"] = max(last_ms, max(m["ms"] for m in msgs))
        if not new or not s["watch"]:
            return
        name = chat["title"] if chat else (new[-1]["author"] or chat_id)
        event = self.store.add_event({
            "type": "message", "chat_id": chat_id, "chat": name, "kind": chat["kind"] if chat else "",
            "handler": s["handler"], "send": s["send"],
            "messages": [{k: m[k] for k in ("mid", "author", "time", "text", "ams", "files") if m.get(k)}
                         for m in new]})
        if s["handler"]:
            self.jobs.put(event)

    def run(self):
        self.store.recover_drafts()
        threading.Thread(target=self._handler_worker, daemon=True).start()
        paused = False
        while not self.stop.is_set():
            if self.store.config().get("paused"):
                paused = True
            else:
                if paused:  # only what comes after a pause counts: no late replies
                    paused = False
                    self.store.save_state({"chats": {}})
                before = self.error
                try:
                    self.poll_once()
                    self.error = None
                except Exception as e:  # keep watching
                    self.error = str(e)[:300]
                if self.error and self.error != before:
                    self.store.add_event({"type": "error", "error": self.error})
            self.stop.wait(POLL_SECONDS)

    # ---------- sending ----------

    def mode(self, chat_id):
        """The chat's send mode now; while paused nothing goes out by itself."""
        mode = self.store.settings(chat_id)["send"]
        return "draft" if mode == "auto" and self.store.config().get("paused") else mode

    def send(self, chat_key, text, files=(), source="agent", confirm_draft=False):
        """Send text to a chat under its send mode.

        Returns {status: sent|draft|refused|not_sent|maybe_sent, ...}. A draft
        waits for a person; confirm_draft=True is only for that person."""
        chat = self.resolve(chat_key)
        text = (text or "").strip()
        files = [str(Path(f).expanduser().resolve()) for f in files or ()]
        missing = [f for f in files if not Path(f).is_file()]
        if missing:
            raise FileNotFoundError("not a file: " + ", ".join(missing))
        home = str(self.store.home.resolve())
        if not confirm_draft and any(f == home or f.startswith(home + os.sep) for f in files):
            raise PermissionError("files from chatwatch's own folder cannot be sent")
        if not text and not files:
            raise ValueError("nothing to send")
        mode = self.mode(chat["id"])
        base = {"chat_id": chat["id"], "chat": chat["title"], "source": source}
        if not confirm_draft:
            if mode == "off":
                self.store.add_event(dict(base, type="refused", text=text))
                return dict(status="refused", reason="sending is off for this chat", chat=chat["title"])
            if mode == "draft":
                d = self.store.add_draft(dict(base, text=text, files=files))
                self.store.add_event(dict(base, type="draft", draft=d["id"], text=text))
                return dict(status="draft", draft=d["id"], chat=chat["title"],
                            reason="waiting for a person to send it")
        def still_auto():  # the mode may change while this send waits for the browser
            if not confirm_draft and self.mode(chat["id"]) != "auto":
                raise NotSent(MODE_CHANGED)

        try:
            msg = self.teams.send(chat["id"], text, files, check=still_auto)
        except NotSent as e:
            if str(e) == MODE_CHANGED:
                return self.send(chat["id"], text, files, source)
            self.store.add_event(dict(base, type="send_failed", error=str(e), text=text))
            return dict(status="not_sent", error=str(e), chat=chat["title"])
        except MaybeSent as e:
            self.store.add_event(dict(base, type="send_uncertain", error=str(e), text=text))
            return dict(status="maybe_sent", error=str(e), chat=chat["title"])
        self.store.add_event(dict(base, type="sent", text=text, files=[Path(f).name for f in files],
                                  mid=msg.get("mid")))
        return dict(status="sent", chat=chat["title"], mid=msg.get("mid"))

    def send_draft(self, draft_id, text=None, checked=False):
        """A person sends a draft (edited text optional). A draft whose earlier
        send may have gone needs checked=True; two sends of one draft at once
        are refused."""
        d = self.store.claim_draft(draft_id, checked)
        text = d["text"] if text is None else text
        try:
            r = self.send(d["chat_id"], text, d.get("files"), source="person", confirm_draft=True)
        except Exception as e:
            self.store.update_draft(draft_id, text=text, sending=False, error=str(e)[:300])
            raise
        if r["status"] == "sent":
            self.store.remove_draft(draft_id)
        elif r["status"] == "maybe_sent":
            self.store.update_draft(draft_id, text=text, sending=False, uncertain=True, error=r["error"])
        else:
            self.store.update_draft(draft_id, text=text, sending=False, error=r.get("error"))
        return r

    # ---------- push handlers ----------

    def _handler_worker(self):
        while not self.stop.is_set():
            try:
                event = self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._handle(event)
            except Exception as e:
                self.store.add_event({"type": "handler_failed", "chat_id": event["chat_id"],
                                      "chat": event["chat"], "handler": event["handler"], "error": str(e)[:300]})

    def _handle(self, event):
        s = self.store.settings(event["chat_id"])
        if s["handler"] != event["handler"] or not s["watch"]:  # changed since the message came
            return
        if self.store.config().get("paused"):
            self.store.add_event({"type": "skipped", "chat_id": event["chat_id"], "chat": event["chat"],
                                  "reason": "paused"})
            return
        h = self.store.handler(event["handler"])
        if not h:
            raise LookupError("no handler named " + event["handler"])
        payload = {"chat_id": event["chat_id"], "chat": event["chat"], "kind": event.get("kind", ""),
                   "messages": event["messages"], "send": s["send"]}
        reply = call_handler(h, payload)
        text, files = reply.get("text", "").strip(), reply.get("files") or []
        if not text and not files:
            self.store.add_event({"type": "no_reply", "chat_id": event["chat_id"], "chat": event["chat"]})
            return
        source = "handler:" + event["handler"]
        draft = {"chat_id": event["chat_id"], "chat": event["chat"], "text": text, "files": files, "source": source}
        # The handler may have taken minutes: if the chat was paused, unwatched
        # or given another handler meanwhile, the reply only waits as a draft.
        s = self.store.settings(event["chat_id"])
        if self.store.config().get("paused") or not s["watch"] or s["handler"] != event["handler"]:
            self.store.add_draft(dict(draft, error="settings changed while the handler worked"))
            return
        try:
            self._deliver(event, text, files, source, draft)
        except Exception as e:  # the reply is never lost
            self.store.add_draft(dict(draft, error="send failed: %s" % str(e)[:300]))

    def _deliver(self, event, text, files, source, draft):
        if self.mode(event["chat_id"]) != "auto":
            self.send(event["chat_id"], text, files, source=source)
            return
        # Auto: a send that certainly did not go is tried again a few times while
        # the chat stays on auto, and only then waits as a draft.
        for wait in AUTO_RETRY_SECONDS + (None,):
            r = self.send(event["chat_id"], text, files, source=source)
            if r["status"] != "not_sent":
                break
            if wait is None or self.mode(event["chat_id"]) != "auto":
                break
            self.store.add_event({"type": "retry", "chat_id": event["chat_id"], "chat": event["chat"],
                                  "error": r["error"], "after": wait})
            if self.stop.wait(wait):
                break
        if r["status"] in ("not_sent", "maybe_sent"):
            self.store.add_draft(dict(draft, uncertain=r["status"] == "maybe_sent", error=r["error"]))


def call_handler(h, payload):
    """Run a handler: {"command": [argv...]} gets the payload as JSON on stdin,
    {"url": "..."} gets it POSTed. Either answers with JSON {"text", "files"}
    or plain text (the reply). Files a handler returns are local paths."""
    timeout = h.get("timeout", HANDLER_TIMEOUT)
    data = json.dumps(payload, ensure_ascii=False)
    if h.get("command"):
        p = subprocess.run(h["command"], input=data, capture_output=True, text=True, timeout=timeout)
        if p.returncode:
            raise RuntimeError("handler exited %d: %s" % (p.returncode, p.stderr.strip()[-300:]))
        out = p.stdout
    elif h.get("url"):
        req = urllib.request.Request(h["url"], data=data.encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = r.read().decode()
    else:
        raise ValueError("a handler needs command or url")
    out = out.strip()
    if out.startswith("{"):
        try:
            v = json.loads(out)
            return {"text": str(v.get("text") or ""), "files": list(v.get("files") or [])}
        except ValueError:
            pass
    return {"text": out, "files": []}


def save_attachments(teams, messages, folder=None):
    """Download a batch's pictures and files into a new folder; return the paths."""
    folder = Path(folder or tempfile.mkdtemp(prefix="chatwatch-"))
    out = []
    pics = teams.images([a for m in messages for a in m.get("ams", [])])
    for i, p in enumerate(pics):
        ext = {"image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}.get(p["type"], ".jpg")
        path = folder / f"picture-{i + 1}{ext}"
        path.write_bytes(base64.b64decode(p["data"]))
        out.append(str(path))
    picture = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic")
    # A picture attached in Teams comes both as a file and in the media store.
    files = [f for m in messages for f in m.get("files", [])
             if not (m.get("ams") and f["name"].lower().endswith(picture))]
    for f in teams.files(files):
        if f.get("data"):
            path = folder / Path(f["name"]).name
            n = 1
            while path.exists():  # two files of the same name
                n += 1
                path = folder / ("%s-%d%s" % (Path(f["name"]).stem, n, Path(f["name"]).suffix))
            path.write_bytes(base64.b64decode(f["data"]))
            out.append(str(path))
    return out
