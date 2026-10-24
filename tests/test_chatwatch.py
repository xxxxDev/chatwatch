"""Tests without a browser: a fake Teams stands in for the tab.

  python3 -m unittest discover tests
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HOME = tempfile.mkdtemp(prefix="chatwatch-test-")
os.environ["CHATWATCH_HOME"] = HOME
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatwatch import http_api, mcp, service  # noqa: E402
from chatwatch.store import Store, api_key  # noqa: E402
from chatwatch.teams import MaybeSent, NotSent  # noqa: E402

service.AUTO_RETRY_SECONDS = (0.01, 0.01, 0.01)


class FakeTeams:
    def __init__(self):
        self.list = [{"id": "19:a", "title": "Alice", "time": "", "preview": "", "kind": "OneOnOne", "unread": False},
                     {"id": "19:b", "title": "Bob Team", "time": "", "preview": "", "kind": "Group", "unread": True}]
        self.convs = {"19:a": "1", "19:b": "1"}
        self.msgs = {"19:a": [], "19:b": []}
        self.sent = []
        self.errors = []   # exceptions raised by the next sends, in order

    def chats(self):
        return self.list

    def conversations(self):
        return [{"id": k, "last_id": v, "last_ms": 0} for k, v in self.convs.items()]

    def messages(self, chat_id, n=20):
        return self.msgs[chat_id][-n:]

    def send(self, chat_id, text, paths=(), check=None):
        if check:
            check()
        if self.errors:
            raise self.errors.pop(0)
        self.sent.append((chat_id, text, list(paths)))
        return {"mid": str(len(self.sent))}

    def incoming(self, chat_id, text, ms):
        mid = str(ms)
        self.msgs[chat_id].append({"mid": mid, "author": "Alice", "time": "", "ms": ms, "mine": False,
                                   "text": text, "ams": [], "files": []})
        self.convs[chat_id] = mid


class Base(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(dir=HOME))
        self.store = Store(self.home)
        self.teams = FakeTeams()
        self.svc = service.Service(self.store, self.teams)


class SendModes(Base):
    def test_default_is_draft(self):
        r = self.svc.send("Alice", "hi")
        self.assertEqual(r["status"], "draft")
        self.assertEqual(self.teams.sent, [])
        self.assertEqual([d["text"] for d in self.store.drafts().values()], ["hi"])

    def test_auto_sends(self):
        self.store.set_chat("19:a", send="auto")
        self.assertEqual(self.svc.send("ali", "hi")["status"], "sent")
        self.assertEqual(self.teams.sent, [("19:a", "hi", [])])

    def test_off_refuses(self):
        self.store.set_chat("19:a", send="off")
        self.assertEqual(self.svc.send("Alice", "hi")["status"], "refused")
        self.assertEqual(self.teams.sent, [])
        self.assertEqual(self.store.drafts(), {})

    def test_unknown_or_ambiguous_chat(self):
        with self.assertRaises(LookupError):
            self.svc.send("nobody", "hi")
        with self.assertRaises(LookupError):
            self.svc.send("e", "hi")  # Alice and Bob Team

    def test_errors_are_reported(self):
        self.store.set_chat("19:a", send="auto")
        self.teams.errors = [NotSent("TYPED_TEXT_MISMATCH: x"), MaybeSent("not seen")]
        self.assertEqual(self.svc.send("Alice", "hi")["status"], "not_sent")
        self.assertEqual(self.svc.send("Alice", "hi")["status"], "maybe_sent")

    def test_missing_file(self):
        self.store.set_chat("19:a", send="auto")
        with self.assertRaises(FileNotFoundError):
            self.svc.send("Alice", "hi", ["/no/such/file"])


class Drafts(Base):
    def test_person_sends_edited_draft(self):
        d = self.svc.send("Alice", "hi")["draft"]
        self.assertEqual(self.svc.send_draft(d, "hello")["status"], "sent")
        self.assertEqual(self.teams.sent, [("19:a", "hello", [])])
        self.assertEqual(self.store.drafts(), {})

    def test_draft_that_may_have_gone_needs_check(self):
        d = self.svc.send("Alice", "hi")["draft"]
        self.teams.errors = [MaybeSent("not seen")]
        self.assertEqual(self.svc.send_draft(d)["status"], "maybe_sent")
        with self.assertRaises(PermissionError):
            self.svc.send_draft(d)
        self.assertEqual(self.svc.send_draft(d, checked=True)["status"], "sent")

    def test_failed_draft_stays(self):
        d = self.svc.send("Alice", "hi")["draft"]
        self.teams.errors = [NotSent("NO_COMPOSE_BOX")]
        self.assertEqual(self.svc.send_draft(d)["status"], "not_sent")
        self.assertIn(d, self.store.drafts())


class Watching(Base):
    def test_first_poll_is_baseline_then_new_messages(self):
        self.svc.poll_once()
        self.assertEqual(self.store.events_after(0)[0], [])
        self.teams.incoming("19:a", "hello", 100)
        self.svc.poll_once()
        events, seq = self.store.events_after(0, {"message"})
        self.assertEqual([m["text"] for m in events[0]["messages"]], ["hello"])
        self.svc.poll_once()  # nothing new
        self.assertEqual(self.store.events_after(seq)[0], [])

    def test_unwatched_chat_is_quiet(self):
        self.svc.poll_once()
        self.store.set_chat("19:a", watch=False)
        self.teams.incoming("19:a", "hello", 100)
        self.svc.poll_once()
        self.assertEqual(self.store.events_after(0)[0], [])


def handler_command(reply):
    return [sys.executable, "-c", "import json,sys; d=json.load(sys.stdin); print(json.dumps(%r))" % reply]


class Push(Base):
    def setUp(self):
        super().setUp()
        cfg = {"handlers": {"echo": {"command": [sys.executable, "-c",
               "import json,sys; d=json.load(sys.stdin); print('got: ' + d['messages'][-1]['text'])"]}}}
        (self.home / "config.json").write_text(json.dumps(cfg))
        self.store.set_chat("19:a", handler="echo")
        self.svc.poll_once()

    def push(self, text="hello"):
        self.ms = getattr(self, "ms", 100) + 1
        self.teams.incoming("19:a", text, self.ms)
        self.svc.poll_once()
        self.svc._handle(self.svc.jobs.get_nowait())

    def test_reply_becomes_draft_by_default(self):
        self.push()
        self.assertEqual([d["text"] for d in self.store.drafts().values()], ["got: hello"])
        self.assertEqual(self.teams.sent, [])

    def test_auto_reply_is_sent(self):
        self.store.set_chat("19:a", send="auto")
        self.push()
        self.assertEqual(self.teams.sent, [("19:a", "got: hello", [])])

    def test_auto_reply_not_sent_is_retried_then_draft(self):
        self.store.set_chat("19:a", send="auto")
        self.teams.errors = [NotSent("x"), NotSent("x")]
        self.push()
        self.assertEqual(len(self.teams.sent), 1)
        self.teams.errors = [NotSent("x")] * 4
        self.push("again")
        self.assertEqual([d["text"] for d in self.store.drafts().values()], ["got: again"])

    def test_auto_reply_maybe_sent_is_not_retried(self):
        self.store.set_chat("19:a", send="auto")
        self.teams.errors = [MaybeSent("x")]
        self.push()
        self.assertEqual(self.teams.sent, [])
        [d] = self.store.drafts().values()
        self.assertTrue(d["uncertain"])

    def test_json_reply_and_empty_reply(self):
        self.assertEqual(service.call_handler({"command": handler_command({"text": "a", "files": ["/x"]})}, {}),
                         {"text": "a", "files": ["/x"]})
        self.assertEqual(service.call_handler({"command": [sys.executable, "-c", "pass"]}, {})["text"], "")


class ReviewFixes(Base):
    """Each failure found in the 2026-10-24 review."""

    def test_empty_key_file_is_refused(self):
        from chatwatch import store
        (self.home / "api-key").write_text("")
        with self.assertRaises(SystemExit):
            store.api_key(self.home)

    def test_one_chat_failing_does_not_repeat_another(self):
        self.svc.poll_once()
        self.teams.incoming("19:a", "once", 100)
        self.teams.incoming("19:b", "b", 100)
        real = self.teams.messages
        self.teams.messages = lambda cid, n=20: (_ for _ in ()).throw(RuntimeError("429")) if cid == "19:b" else real(cid, n)
        with self.assertRaises(RuntimeError):
            self.svc.poll_once()
        self.teams.messages = real
        self.svc.poll_once()
        texts = [m["text"] for e in self.store.events_after(0, {"message"})[0] for m in e["messages"]]
        self.assertEqual(sorted(texts), ["b", "once"])

    def test_handler_reply_after_pause_waits_as_draft(self):
        cfg = {"handlers": {"h": {"command": [sys.executable, "-c", "print('hi')"]}}}
        (self.home / "config.json").write_text(json.dumps(cfg))
        self.store.set_chat("19:a", handler="h", send="auto")
        event = {"chat_id": "19:a", "chat": "Alice", "handler": "h", "messages": [{"text": "x"}]}
        orig = service.call_handler
        service.call_handler = lambda h, p: (self.store.set_paused(True), {"text": "hi", "files": []})[1]
        try:
            self.svc._handle(event)
        finally:
            service.call_handler = orig
        self.assertEqual(self.teams.sent, [])
        self.assertEqual([d["text"] for d in self.store.drafts().values()], ["hi"])
        self.svc._handle(event)  # still paused: skipped
        self.assertEqual(len(self.store.drafts()), 1)

    def test_auto_send_while_paused_is_a_draft(self):
        self.store.set_chat("19:a", send="auto")
        self.store.set_paused(True)
        self.assertEqual(self.svc.send("Alice", "hi")["status"], "draft")

    def test_mode_changed_while_waiting_makes_a_draft(self):
        self.store.set_chat("19:a", send="auto")
        real = self.teams.send
        def slow(chat_id, text, paths=(), check=None):
            self.store.set_chat("19:a", send="draft")  # changed while waiting for the browser
            return real(chat_id, text, paths, check)
        self.teams.send = slow
        self.assertEqual(self.svc.send("Alice", "hi")["status"], "draft")
        self.assertEqual(self.teams.sent, [])

    def test_error_after_press_is_maybe_sent(self):
        from chatwatch import teams
        t = teams.Teams(self.home)
        def after_press(chat_id, text, paths, pressed):
            pressed.append(True)
            raise TimeoutError("Chrome did not answer")
        def before_press(chat_id, text, paths, pressed):
            raise TimeoutError("Chrome did not answer")
        t._send = after_press
        with self.assertRaises(MaybeSent):
            t.send("19:a", "hi")
        t._send = before_press
        with self.assertRaises(NotSent):
            t.send("19:a", "hi")

    def test_id_wins_over_a_title_that_looks_like_it(self):
        self.teams.list.insert(0, {"id": "19:evil", "title": "19:a", "time": "", "preview": "", "kind": "Group",
                                   "unread": False})
        self.assertEqual(self.svc.resolve("19:a")["title"], "Alice")
        self.teams.list.append(dict(self.teams.list[1], id="19:a2"))  # a second "Alice"
        with self.assertRaises(LookupError):
            self.svc.resolve("Alice")

    def test_broken_config_is_not_overwritten(self):
        (self.home / "config.json").write_text('{"handlers": {"h": {}},}')
        with self.assertRaises(ValueError):
            self.store.set_chat("19:a", send="auto")
        self.assertIn('"h"', (self.home / "config.json").read_text())

    def test_draft_cannot_be_sent_twice_at_once(self):
        d = self.svc.send("Alice", "hi")["draft"]
        self.store.claim_draft(d)
        with self.assertRaises(PermissionError):
            self.svc.send_draft(d)
        self.store.recover_drafts()  # e.g. a restart mid-send
        self.assertTrue(self.store.drafts()[d]["uncertain"])
        with self.assertRaises(PermissionError):
            self.svc.send_draft(d)
        self.assertEqual(self.svc.send_draft(d, checked=True)["status"], "sent")
        self.assertEqual(len(self.teams.sent), 1)

    def test_damaged_event_line_does_not_rewind(self):
        self.store.add_event({"type": "x"})
        with open(self.home / "events.jsonl", "a") as f:
            f.write("not json\n")
        self.store.add_event({"type": "x"})
        self.assertEqual(Store(self.home).seq, 2)

    def test_own_folder_files_are_not_sent_by_agents(self):
        self.store.set_chat("19:a", send="auto")
        self.store.add_draft({"text": "x"})
        with self.assertRaises(PermissionError):
            self.svc.send("Alice", "here", [str(self.home / "drafts.json")])


class LiveView(Base):
    def test_input_waits_briefly_for_a_send_then_gives_up(self):
        from chatwatch import teams
        t = teams.Teams(self.home)
        held, release = threading.Event(), threading.Event()
        def sending():
            with t.busy():
                held.set()
                release.wait(5)
        threading.Thread(target=sending).start()
        held.wait(2)
        start = time.time()
        with self.assertRaises(TimeoutError):
            with t.busy(timeout=0.3):
                pass
        self.assertLess(time.time() - start, 2)
        release.set()

    def test_unknown_input_is_refused(self):
        from chatwatch import teams
        t = teams.Teams(self.home)
        t._tab = type("T", (), {"cdp": type("C", (), {"closed": False})()})()
        with self.assertRaises(ValueError):
            t.operate({"type": "navigate", "url": "https://example.com"})


class Api(Base):
    @classmethod
    def setUpClass(cls):
        cls.home = Path(HOME)
        cls.store = Store(cls.home)
        cls.teams = FakeTeams()
        cls.svc = service.Service(cls.store, cls.teams)
        cls.server = http_api.serve(cls.svc, 0)
        cls.port = cls.server.server_address[1]
        mcp.URL = "http://127.0.0.1:%d" % cls.port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.key = api_key(cls.home)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        pass

    def req(self, path, body=None, key=True, host=None):
        h = {"Content-Type": "application/json"}
        if key:
            h["X-Chatwatch-Key"] = self.key
        if host:
            h["Host"] = host
        r = urllib.request.Request("http://127.0.0.1:%d" % self.port + path, headers=h, method="POST" if body is not None else "GET",
                                   data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_key_and_host_are_checked(self):
        self.assertEqual(self.req("/api/status", key=False)[0], 401)
        self.assertEqual(self.req("/api/status", host="evil.example")[0], 403)
        self.assertEqual(self.req("/api/status")[0], 200)

    def test_mcp_tools(self):
        tools = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertIn("teams_send_message", [t["name"] for t in tools])
        r = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "teams_send_message", "arguments": {"chat": "Bob", "text": "yo"}}})
        self.assertEqual(json.loads(r["result"]["content"][0]["text"])["status"], "draft")
        r = mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "teams_read_messages", "arguments": {"chat": "nobody"}}})
        self.assertTrue(r["result"]["isError"])

    def test_mcp_wait_returns_new_messages(self):
        self.svc.poll_once()
        start = self.store.seq
        threading.Timer(0.3, lambda: (self.teams.incoming("19:b", "ping", 500), self.svc.poll_once())).start()
        t = time.time()
        r = mcp.call_tool("teams_wait_for_messages", {"after_seq": start, "wait_seconds": 5})
        self.assertLess(time.time() - t, 4)
        self.assertEqual(r["events"][0]["messages"][0]["text"], "ping")
        self.assertEqual(mcp.call_tool("teams_wait_for_messages", {"after_seq": r["seq"], "wait_seconds": 0})["events"], [])


if __name__ == "__main__":
    unittest.main()
