"""Read and send Microsoft Teams chats through the Teams web tab.

Reading uses Teams' own chat service API, called from inside the tab with the
access token Teams keeps there. Reading that way never opens a chat, so
nothing is marked read. The token stays in the tab: it is never returned to
Python, printed or written to disk.

Sending works like a person: open the chat, paste the whole text into the
compose box, attach files, press send and wait for the message to appear.
Opening a chat marks it read in Teams; that cannot be avoided when sending.

Every browser step holds one lock (a thread lock plus a file lock), so the
daemon and a command line call never drive the tab at the same time.
"""
import json
import re
import threading
import time
from contextlib import contextmanager

from . import browser, cdp
from .store import HOME

try:
    import fcntl
except ImportError:  # Windows: the thread lock alone
    fcntl = None

MAX_IMAGES = 4
MAX_FILES = 4
MAX_FILE_BYTES = 20 * 1024 * 1024


class NotSent(RuntimeError):
    """The send stopped before anything could have reached the chat."""


class MaybeSent(RuntimeError):
    """Send was pressed but the message was not seen in the chat: check by hand."""


LIST_JS = r"""
(()=>[...document.querySelectorAll('[role=treeitem][data-item-type=chat]')].map(e=>{
  const v=e.getAttribute('data-fui-tree-item-value')||'';
  const id=v.slice(v.lastIndexOf('|')+1);
  const q=p=>{const n=document.getElementById(p+id);return n?n.innerText.trim():''};
  const kind=(v.match(/OneGQL_(\w+?)Conversation\|[^|]*$/)||[])[1]||'';
  const unread=(e.getAttribute('aria-labelledby')||'').includes('chat_list_unread_text');
  return {id, title:q('title-chat-list-item_'), time:q('time-chat-list-item_'),
          preview:q('message-preview-chat-list-item_'), kind, unread};
}))()
"""

MESSAGES_JS = r"""
(()=>{let last='';return [...document.querySelectorAll('[data-tid=chat-pane-item]')].map(it=>{
  const m=it.querySelector('[data-tid=chat-pane-message]'); if(!m) return null;
  const a=it.querySelector('[data-tid=message-author-name]'); if(a) last=a.innerText.trim();
  return {mid:m.getAttribute('data-mid'), author:last,
          time:it.querySelector('time')?.getAttribute('datetime')||'',
          mine:m.className.includes('ChatMyMessage'), text:m.innerText.trim()};
}).filter(Boolean)})()
"""

# Runs inside the Teams tab. Finds Teams' own chat-service access token and
# regional endpoint, and exposes GET-only helpers; the token never leaves.
API_JS = r"""
const __msta = (() => {
  let tok = null, exp = 0, base = null;
  for (const k of Object.keys(localStorage)) {
    try {
      if (k.includes('accesstoken')) {
        const v = JSON.parse(localStorage.getItem(k));
        if (v.target && v.target.includes('ic3.teams.office.com/Teams.AccessAsUser.All') && v.secret && Number(v.expiresOn) > exp) {
          tok = v.secret; exp = Number(v.expiresOn);
        }
      } else if (k.endsWith('Discover.DISCOVER-REGION-GTM')) {
        base = JSON.parse(localStorage.getItem(k)).item.chatServiceAfd;
      }
    } catch (e) {}
  }
  if (!tok || exp * 1000 < Date.now()) throw new Error('NO_TEAMS_TOKEN');
  if (!base || !base.startsWith(location.origin + '/')) throw new Error('NO_CHAT_SERVICE');
  let oid = '';
  try { oid = JSON.parse(atob(tok.split('.')[1].replace(/-/g, '+').replace(/_/g, '/'))).oid || ''; } catch (e) {}
  const get = async (path) => {
    const r = await fetch(base + path, {headers: {authorization: 'Bearer ' + tok}});
    if (!r.ok) throw new Error('CHAT_API_' + r.status);
    return r.json();
  };
  // Trusted Types forbid DOMParser here, so strip tags by hand.
  const ents = {nbsp: ' ', amp: '&', lt: '<', gt: '>', quot: '"', apos: "'"};
  const text = (html) => (html || '')
    // Keep a link's address, not only its words.
    .replace(/<a\b[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/gi, (m, href, inner) => {
      const words = inner.replace(/<[^>]*>/g, '').trim(), url = href.replace(/&amp;/g, '&');
      return words && words !== url ? words + ' (' + url + ')' : url; })
    .replace(/<br\s*\/?>/gi, '\n').replace(/<\/(p|div|li|blockquote)>/gi, '\n')
    .replace(/<[^>]*>/g, '')
    .replace(/&(#x[0-9a-f]+|#\d+|\w+);/gi, (m, e) => e[0] === '#'
      ? String.fromCodePoint(e[1].toLowerCase() === 'x' ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10))
      : (ents[e.toLowerCase()] ?? m))
    .replace(/\r/g, '').replace(/\n{3,}/g, '\n\n').trim();
  const mine = (from) => !!oid && (from || '').endsWith(oid);
  return {get, text, mine};
})();
"""


def in_page(statements):
    """Wrap page code so __msta (and the token) stay local to one call."""
    return "(async () => {" + API_JS + statements + "})()"


CONVERSATIONS_JS = in_page(r"""
return (await __msta.get('/v1/users/ME/conversations?view=mychats&pageSize=100')).conversations
  .filter(c => c.id.startsWith('19:') && c.lastMessage && c.lastMessage.id)
  .map(c => ({id: c.id, last_id: String(c.lastMessage.id),
              last_ms: Date.parse(c.lastMessage.composetime) || 0}));
""")

MESSAGES_API_JS = r"""
return (await __msta.get('/v1/users/ME/conversations/' + encodeURIComponent(%s)
    + '/messages?view=msnp24Equivalent|supportsMessageProperties&pageSize=%d&startTime=1')).messages
  .filter(m => /^(Text|RichText)/.test(m.messagetype || '') && !m.properties?.deletetime)
  .map(m => {
    // Pictures pasted into a message live in Teams' media store (amsreferences);
    // attached files live in SharePoint and are only named here.
    let files = [];
    try { files = JSON.parse(m.properties?.files || '[]').filter(f => f.fileName && f.fileInfo?.fileUrl)
      .map(f => ({name: f.fileName, url: f.fileInfo.fileUrl})); } catch (e) {}
    const ams = (m.amsreferences || []).map(String);
    const text = [__msta.text(m.content), ...files.map(f => '[file] ' + f.name)].filter(Boolean).join('\n');
    return {mid: String(m.id), author: m.imdisplayname || '', time: m.composetime,
            ms: Date.parse(m.composetime) || 0, mine: __msta.mine(m.from),
            text: text || (ams.length ? '[picture]' : ''), ams, files};
  })
  .reverse();
"""

# Pictures from Teams' media store, fetched inside the tab (its own cookies; no
# token involved) and returned as base64. Full size, or the preview when large.
IMAGES_JS = r"""
(async (ids) => {
  let base = 'https://us-prod.asyncgw.teams.microsoft.com';
  for (const k of Object.keys(localStorage)) if (k.endsWith('Discover.DISCOVER-REGION-GTM')) {
    try { base = JSON.parse(localStorage.getItem(k)).item.amsV2 || base; } catch (e) {} }
  const out = [];
  for (const id of ids) {
    for (const view of ['imgpsh_fullsize_anim', 'imgo']) {
      const r = await fetch(base + '/v1/objects/' + encodeURIComponent(id) + '/views/' + view, {credentials: 'include'});
      if (!r.ok) continue;
      const b = await r.blob();
      if (!b.type.startsWith('image/') || b.size > 4.5e6) continue;
      const data = await new Promise(res => { const f = new FileReader(); f.onload = () => res(String(f.result).split(',')[1]); f.readAsDataURL(b); });
      out.push({id, type: b.type, data}); break;
    }
  }
  return out;
})
"""

# Files attached in Teams live in SharePoint/OneDrive. Downloaded inside the tab
# through Microsoft Graph's sharing link lookup with the Graph token Teams keeps
# there (it never leaves the tab); returns [{name, type, data(base64)}].
FILES_JS = r"""
(async (files, maxBytes) => {
  let tok = null, exp = 0;
  for (const k of Object.keys(localStorage)) {
    if (!k.includes('accesstoken')) continue;
    try { const v = JSON.parse(localStorage.getItem(k));
      if (v.target && v.target.includes('graph.microsoft.com') && Number(v.expiresOn) > exp) { tok = v.secret; exp = Number(v.expiresOn); } } catch (e) {}
  }
  if (!tok) throw new Error('NO_GRAPH_TOKEN');
  const out = [];
  for (const f of files) {
    const share = 'u!' + btoa(unescape(encodeURIComponent(f.url))).replace(/=+$/, '').replace(/\//g, '_').replace(/\+/g, '-');
    const r = await fetch('https://graph.microsoft.com/v1.0/shares/' + share + '/driveItem/content', {headers: {authorization: 'Bearer ' + tok}});
    if (!r.ok) { out.push({name: f.name, error: 'HTTP ' + r.status}); continue; }
    const b = await r.blob();
    if (b.size > maxBytes) { out.push({name: f.name, error: 'too big'}); continue; }
    const data = await new Promise(res => { const x = new FileReader(); x.onload = () => res(String(x.result).split(',')[1]); x.readAsDataURL(b); });
    out.push({name: f.name, type: b.type || 'application/octet-stream', data});
  }
  return out;
})
"""

COMPOSE = "[data-tid=ckeditor]"
SEND_BTN = "[data-tid=sendMessageCommands-send],[data-tid=newMessageCommands-send]"

# Wait until every attachment has finished uploading and Teams lets the message
# go: the file chip is there, nothing in the compose area is still in progress,
# and the send button is enabled.
READY_JS = r"""(names) => {
  const e = document.querySelector('[data-tid=ckeditor]'); if (!e) return false;
  let box = e; for (let i = 0; i < 10 && box; i++) { box = box.parentElement; if (box && box.querySelector('[data-tid=sendMessageCommands-send],[data-tid=newMessageCommands-send]')) break; }
  if (!box) return false;
  const wells = [...box.querySelectorAll('[data-tid=file-well]')];
  const text = wells.map(w => w.innerText).join(' ');
  if (!names.every(n => text.includes(n))) return false;
  if (box.querySelector('[role=progressbar],[aria-busy=true]')) return false;
  const b = box.querySelector('[data-tid=sendMessageCommands-send],[data-tid=newMessageCommands-send]');
  return !!b && !b.disabled && b.getAttribute('aria-disabled') !== 'true';
}"""

# Hands the editor a paste of plain text (its own paste handling, no clipboard):
# it keeps line breaks and leaves ``` alone. Typing key by key now and then
# loses a key.
PASTE_JS = r"""(t) => {
  const e = document.querySelector('[data-tid=ckeditor]'); e.focus();
  const dt = new DataTransfer(); dt.setData('text/plain', t);
  e.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
  return true;
}"""

CENTER_JS = r"""(sel) => {
  const e = document.querySelector(sel); if (!e) return null;
  e.scrollIntoView({block: 'center'}); const r = e.getBoundingClientRect();
  return r.width ? [r.x + r.width / 2, r.y + r.height / 2] : null;
}"""

SELECT_ALL_JS = r"""(() => {const e = document.querySelector('[data-tid=ckeditor]'); e.focus();
  const r = document.createRange(); r.selectNodeContents(e); const s = getSelection();
  s.removeAllRanges(); s.addRange(r)})()"""


def bare(s):
    """Text without whitespace: Teams renders paragraphs with extra blank lines."""
    return re.sub(r"[\s⁠​]+", "", s or "")


class Teams:
    def __init__(self, home=None):
        self.lock = threading.RLock()
        self.lock_path = (home or HOME) / "browser.lock"
        self._tab = None

    @contextmanager
    def busy(self, timeout=None):
        """The browser for one step; with timeout, raises TimeoutError if it
        stays taken (a send can hold it for minutes)."""
        if not self.lock.acquire(timeout=-1 if timeout is None else timeout):
            raise TimeoutError("the Teams tab is busy sending; try again in a moment")
        try:
            if fcntl is None:
                yield
                return
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.lock_path, "w") as f:
                flags = fcntl.LOCK_EX if timeout is None else fcntl.LOCK_EX | fcntl.LOCK_NB
                deadline = time.time() + (timeout or 0)
                while True:
                    try:
                        fcntl.flock(f, flags)
                        break
                    except BlockingIOError:
                        if time.time() > deadline:
                            raise TimeoutError("the Teams tab is busy sending; try again in a moment")
                        time.sleep(0.1)
                try:
                    yield
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            self.lock.release()

    def tab(self):
        if self._tab is None or self._tab.cdp.closed:
            self._tab = cdp.Tab(browser.teams_target())
        return self._tab

    def js(self, expression, timeout=cdp.CALL_TIMEOUT):
        try:
            return self.tab().js(expression, timeout)
        except ConnectionError:
            self._tab = None
            return self.tab().js(expression, timeout)

    # ---- the live view (the page's Teams screen) ----

    def screen(self):
        """A picture of the Teams tab, for a person to see what it shows (a
        sign-in page, a prompt). Taken without the lock: it changes nothing."""
        try:
            t = self.tab()
            if t.js("document.visibilityState") != "visible":
                t.bring_to_front()  # headless Chrome paints only the front tab
            host = t.js("location.host")
            data, width, height = t.screenshot()
        except ConnectionError:
            self._tab = None
            raise
        return {"data": data, "width": width, "height": height, "host": host}

    def operate(self, e):
        """One input from the person on the live view: click, wheel, text, key
        or reload. x and y are in the page's CSS pixels. Waits briefly for a send
        in progress rather than typing into it."""
        kind = e.get("type")
        with self.busy(timeout=5):
            t = self.tab()
            if kind == "click":
                t.click(float(e["x"]), float(e["y"]))
            elif kind == "wheel":
                t.wheel(float(e["x"]), float(e["y"]), float(e.get("dx", 0)), float(e.get("dy", 0)))
            elif kind == "text":
                t.text(str(e.get("text", ""))[:2000])
            elif kind == "key":
                t.key(str(e.get("key")), int(e.get("modifiers", 0)))
            elif kind == "reload":
                t.cdp.call("Page.reload")
            else:
                raise ValueError("unknown input " + str(kind))
        return {"ok": True}

    # ---- reading (never opens a chat) ----

    def chats(self):
        with self.busy():
            return self.js(LIST_JS) or []

    def conversations(self):
        """Chat id -> last message id and time, from the chat service."""
        with self.busy():
            return self.js(CONVERSATIONS_JS)

    def messages(self, chat_id, n=20):
        """Last n real messages of a chat, oldest first, without opening it."""
        with self.busy():
            return self.js(in_page(MESSAGES_API_JS % (json.dumps(chat_id), int(n))))

    def images(self, ids):
        ids = list(ids)[:MAX_IMAGES]
        if not ids:
            return []
        with self.busy():
            return self.tab().call(IMAGES_JS, ids, timeout=120)

    def files(self, files):
        files = list(files)[:MAX_FILES]
        if not files:
            return []
        with self.busy():
            return self.tab().call(FILES_JS, files, MAX_FILE_BYTES, timeout=300)

    def resolve(self, key):
        """A chat by id, exact title, or a title fragment matching only one."""
        chats = self.chats()
        for c in chats:
            if key in (c["id"], c["title"]):
                return c
        hits = [c for c in chats if key.lower() in c["title"].lower()]
        if len(hits) == 1:
            return hits[0]
        raise LookupError(("several chats match: " if hits else "no chat matches: ") + key)

    # ---- sending ----

    def _open_chat(self, t, chat_id):
        t.bring_to_front()
        time.sleep(0.3)
        find = ('(()=>{const e=[...document.querySelectorAll("[role=treeitem][data-item-type=chat]")]'
                '.find(e=>(e.getAttribute("data-fui-tree-item-value")||"").endsWith("|"+%s));'
                'if(!e) return false; e.click(); return true})()' % json.dumps(chat_id))
        for _ in range(60):  # brought forward, the chat list can take a few seconds to draw
            if t.js(find):
                break
            time.sleep(0.25)
        else:
            raise NotSent("CHAT_NOT_FOUND")
        want = t.js('document.getElementById(%s)?.innerText.trim()' % json.dumps("title-chat-list-item_" + chat_id))
        for _ in range(240):  # a chat can take 20 s or more right after a reload
            title = t.js('document.querySelector("[data-tid=chat-title]")?.innerText.trim()')
            if title == want and t.js('document.querySelectorAll("[data-tid=chat-pane-message]").length'):
                break
            time.sleep(0.25)
        else:
            raise NotSent("CHAT_DID_NOT_OPEN")
        time.sleep(0.5)

    def _box(self, t):
        return t.js('document.querySelector("[data-tid=ckeditor]")?.innerText') or ""

    def _discard(self, t):
        """Throw away typed text and attachments, so nothing is left to send by hand."""
        t.js('(()=>{const b=document.querySelector("[data-tid=newMessageCommands-discard-draft],'
             '[data-tid=sendMessageCommands-discard-draft]");if(b)b.click()})()')
        time.sleep(0.8)
        t.js('(()=>{const b=document.querySelector("[data-tid=messagedraft-discard-confirm]");if(b)b.click()})()')
        time.sleep(0.8)
        # Labels are matched in English and in Traditional Chinese (the Teams UI
        # language chatwatch was built with); add others here.
        for _ in range(10):
            if not t.js('(()=>{const b=[...document.querySelectorAll("[data-tid=file-well] button")]'
                        '.find(x=>/移除附件|Remove attachment/i.test(x.getAttribute("aria-label")||""));'
                        'if(!b)return false;b.click();return true})()'):
                break
            time.sleep(0.5)
        if bare(self._box(t)):
            t.js(SELECT_ALL_JS)
            time.sleep(0.2)
            t.key("Backspace")
            time.sleep(0.3)

    def _attach(self, t, paths):
        """Attach files → Upload from this device, answering the file
        chooser through DevTools (Teams ignores a scripted paste or drop)."""
        c = t.cdp
        c.call("Page.enable")
        c.call("DOM.enable")
        c.call("Page.setInterceptFileChooserDialog", {"enabled": True})
        c.drain_events()
        try:
            pick = t.call(CENTER_JS, "[data-tid=sendMessageCommands-FilePicker],[data-tid=newMessageCommands-FilePicker]")
            if not pick:
                raise NotSent("NO_FILE_PICKER")
            t.click(*pick)
            time.sleep(1.2)
            item = t.js('(()=>{const e=[...document.querySelectorAll("[role=menuitem]")].find(e=>'
                        '/從此裝置上傳|Upload from this device/.test(e.innerText||""));if(!e)return null;'
                        'const r=e.getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]})()')
            if not item:
                t.key("Escape")
                raise NotSent("NO_UPLOAD_ITEM")
            t.click(*item)
            ev = None
            for _ in range(40):
                time.sleep(0.25)
                ev = next((e for e in c.drain_events() if e.get("method") == "Page.fileChooserOpened"), None)
                if ev:
                    break
            if not ev:
                raise NotSent("NO_FILE_CHOOSER")
            c.call("DOM.setFileInputFiles", {"files": [str(p) for p in paths],
                                             "backendNodeId": ev["params"]["backendNodeId"]})
        finally:
            c.call("Page.setInterceptFileChooserDialog", {"enabled": False})
        names = [str(p).replace("\\", "/").rsplit("/", 1)[-1] for p in paths]
        for _ in range(480):  # up to two minutes for the upload to OneDrive
            time.sleep(0.25)
            if t.call(READY_JS, names):
                break
        else:
            raise NotSent("ATTACH_NOT_READY")
        time.sleep(1.5)
        return names

    def send(self, chat_id, text, paths=(), check=None):
        """Send text (and local files) to a chat; return the posted message.

        check() runs once the browser is ours, just before anything is typed,
        and raises NotSent to stop (e.g. the chat's send mode changed while
        this send waited). Raises NotSent when nothing can have gone out,
        MaybeSent when send was pressed but the message was not seen; any
        other error is turned into one of the two."""
        with self.busy():
            if check:
                check()
            pressed = []
            try:
                return self._send(chat_id, text, list(paths), pressed)
            except (NotSent, MaybeSent):
                raise
            except Exception as e:
                if pressed:
                    raise MaybeSent("error after send was pressed: %s" % e) from e
                raise NotSent("%s: %s" % (type(e).__name__, e)) from e

    def _send(self, chat_id, text, paths, pressed):
        t = self.tab()
        self._open_chat(t, chat_id)
        before = {m["mid"] for m in t.js(MESSAGES_JS) or []}
        for _ in range(40):  # the compose box can draw a moment after the chat
            ed = t.call(CENTER_JS, COMPOSE)
            if ed:
                break
            time.sleep(0.25)
        else:
            raise NotSent("NO_COMPOSE_BOX")
        t.click(*ed)
        time.sleep(0.3)
        names = [str(p).replace("\\", "/").rsplit("/", 1)[-1] for p in paths]
        left = self._box(t)
        wells = t.js('[...document.querySelectorAll("[data-tid=file-well]")]'
                     '.map(w=>w.innerText.split("\\n")[0].trim())') or []
        # Our own earlier attempt that did not go (same text, our files): clear
        # it and redo. Anything else is someone's draft and is left alone.
        if (bare(left) or wells) and bare(left) in ("", bare(text)) and set(wells) <= set(names):
            self._discard(t)
        elif bare(left) or wells:
            raise NotSent("DRAFT_NOT_EMPTY")
        try:
            for attempt in range(3):
                t.call(PASTE_JS, text)
                time.sleep(0.6)
                typed = self._box(t)
                if bare(typed) == bare(text):
                    break
                self._discard(t)
                time.sleep(0.5)
                t.click(*ed)
                time.sleep(0.3)
            else:
                raise NotSent("TYPED_TEXT_MISMATCH: " + typed[:150])
            pastes = attempt + 1
            if paths:
                self._attach(t, paths)
        except BaseException:
            self._discard(t)
            raise
        want = bare(text)
        press = '(()=>{const b=document.querySelector(%s);if(!b)return false;b.click();return true})()' % json.dumps(SEND_BTN)
        pressed.append(True)
        if not t.js(press):
            pressed.clear()
            self._discard(t)
            raise NotSent("NO_SEND_BUTTON")
        for attempt in range(3):
            for _ in range(40):
                time.sleep(0.25)
                new = [m for m in t.js(MESSAGES_JS) or [] if m["mine"] and m["mid"] not in before]
                if new:
                    return dict(new[-1], pastes=pastes)
            # Still in the compose box: Teams did not take the click (an upload
            # not quite done, the button busy). It did not go, so press again.
            if bare(self._box(t)) != want:
                break
            for _ in range(80):
                if t.call(READY_JS, names):
                    break
                time.sleep(0.25)
            if attempt < 2:
                t.js(press)
        if bare(self._box(t)) == want:
            self._discard(t)
            raise NotSent("SEND_NOT_TAKEN")
        raise MaybeSent("send was pressed but the message did not show up in the chat")
