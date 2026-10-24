"""Files under CHATWATCH_HOME (default ~/.chatwatch).

  config.json     per-chat settings and push handlers (see chat_settings)
  state.json      last message seen per chat
  events.jsonl    every new message and send result, numbered (seq)
  drafts.json     replies waiting for a person to send or discard
  api-key         the key local clients send in X-Chatwatch-Key
"""
import json
import os
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: the thread lock alone
    fcntl = None

HOME = Path(os.environ.get("CHATWATCH_HOME", Path.home() / ".chatwatch"))
SEND_MODES = ("off", "draft", "auto")


def load_json(path, default):
    """The file's value, or default when it does not exist. A file that is not
    valid JSON raises: it may be hand-edited, and must not be written over."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError("%s is not valid JSON (%s); fix it by hand" % (path, e)) from None


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(value, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def make_home(home):
    """CHATWATCH_HOME, readable only by this user (drafts, events, Chrome profile)."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    return home


MIN_KEY = 20


def api_key(home=None):
    """The local API key, made on first use and readable only by this user.
    Written to a temporary file and linked into place, so no process ever reads
    a half-written key; an empty or short key is refused, never accepted."""
    home = make_home(home or HOME)
    path = home / "api-key"
    if not path.exists():
        fd, tmp = tempfile.mkstemp(dir=home, prefix="api-key.", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(24))
        try:
            os.link(tmp, path)   # fails if another process made it first
        except FileExistsError:
            pass
        finally:
            os.unlink(tmp)
    key = path.read_text().strip()
    if len(key) < MIN_KEY:
        raise SystemExit("%s is empty or too short: delete it and start again" % path)
    return key


def chat_settings(config, chat_id):
    """A chat's settings. New chats are watched; what an agent sends becomes a
    draft until a person sends it, unless the chat is set to auto."""
    s = config.get("chats", {}).get(chat_id, {})
    return {
        "watch": s.get("watch", True),
        "send": s.get("send") if s.get("send") in SEND_MODES else "draft",
        "handler": s.get("handler") or "",
    }


class Store:
    def __init__(self, home=None):
        self.home = make_home(home or HOME)
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.events_path = self.home / "events.jsonl"
        self.recent = []
        self.seq = 0
        try:
            lines = self.events_path.read_text().splitlines()
        except FileNotFoundError:
            lines = []
        for line in lines:  # a damaged line is skipped, never rewinds the count
            try:
                e = json.loads(line)
                self.seq = max(self.seq, int(e.get("seq", 0)))
            except (ValueError, TypeError, AttributeError):
                continue
            self.recent.append(e)
        del self.recent[:-1000]

    @contextmanager
    def file_lock(self):
        """Read-modify-write of config.json, across processes (CLI and daemon)."""
        with self.lock:
            if fcntl is None:
                yield
                return
            with open(self.home / "config.lock", "w") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)

    # config
    def config(self):
        return load_json(self.home / "config.json", {})

    def settings(self, chat_id):
        return chat_settings(self.config(), chat_id)

    def set_chat(self, chat_id, **changes):
        with self.file_lock():
            cfg = self.config()
            entry = cfg.setdefault("chats", {}).setdefault(chat_id, {})
            for k, v in changes.items():
                if k == "send" and v not in SEND_MODES:
                    raise ValueError("send must be one of " + ", ".join(SEND_MODES))
                if k == "handler" and v and v not in cfg.get("handlers", {}):
                    raise ValueError("no handler named " + v)
                entry[k] = v
            save_json(self.home / "config.json", cfg)
            return chat_settings(cfg, chat_id)

    def set_paused(self, paused):
        with self.file_lock():
            cfg = self.config()
            cfg["paused"] = bool(paused)
            save_json(self.home / "config.json", cfg)
            return cfg["paused"]

    def handler(self, name):
        return self.config().get("handlers", {}).get(name)

    # state
    def state(self):
        return load_json(self.home / "state.json", {"chats": {}})

    def save_state(self, state):
        save_json(self.home / "state.json", state)

    # events
    def add_event(self, event):
        with self.lock:
            self.seq += 1
            event = dict(event, seq=self.seq, ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
            with self.events_path.open("a") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
            self.recent.append(event)
            del self.recent[:-1000]
            self.changed.notify_all()
            return event

    def events_after(self, seq, types=None, wait=0.0):
        """(events numbered above seq, the number to ask after next time),
        waiting up to `wait` seconds for one."""
        deadline = time.time() + wait
        with self.lock:
            while True:
                out = [e for e in self.recent if e["seq"] > seq and (not types or e["type"] in types)]
                left = deadline - time.time()
                if out or left <= 0:
                    return out, self.seq
                self.changed.wait(left)

    # drafts
    def drafts(self):
        return load_json(self.home / "drafts.json", {})

    def add_draft(self, draft):
        with self.lock:
            d = self.drafts()
            draft = dict(draft, id=secrets.token_hex(6), created=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
            d[draft["id"]] = draft
            save_json(self.home / "drafts.json", d)
            return draft

    def update_draft(self, draft_id, **changes):
        with self.lock:
            d = self.drafts()
            if draft_id not in d:
                raise KeyError(draft_id)
            d[draft_id].update(changes)
            save_json(self.home / "drafts.json", d)
            return d[draft_id]

    def claim_draft(self, draft_id, checked=False):
        """Mark a draft as being sent and return it; only one send at a time.
        A draft whose earlier send may have gone needs checked=True."""
        with self.lock:
            d = self.drafts()
            if draft_id not in d:
                raise KeyError("no draft " + draft_id)
            if d[draft_id].get("sending"):
                raise PermissionError("this draft is being sent already")
            if d[draft_id].get("uncertain") and not checked:
                raise PermissionError("this draft may already have been sent; check the chat first")
            d[draft_id]["sending"] = True
            save_json(self.home / "drafts.json", d)
            return dict(d[draft_id])

    def recover_drafts(self):
        """After a restart, a draft left mid-send may have gone: needs checking."""
        with self.lock:
            d = self.drafts()
            for v in d.values():
                if v.pop("sending", None):
                    v["uncertain"] = True
                    v["error"] = "chatwatch stopped while sending this; check the chat"
            save_json(self.home / "drafts.json", d)

    def remove_draft(self, draft_id):
        with self.lock:
            d = self.drafts()
            gone = d.pop(draft_id, None)
            save_json(self.home / "drafts.json", d)
            return gone
