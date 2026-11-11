"""Start or find the Chrome that holds the Teams tab.

chatwatch uses its own Chrome profile (CHATWATCH_HOME/chrome-profile), so the
Teams sign-in lives there and nowhere else. Run `chatwatch browser --show`
once to sign in with a visible window; afterwards it can run headless.

To use a Chrome you already run with --remote-debugging-port, set
CHATWATCH_CDP=http://127.0.0.1:<port>; chatwatch then never starts one.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from . import cdp
from .store import HOME

PORT = int(os.environ.get("CHATWATCH_CDP_PORT", "9334"))
CDP_URL = os.environ.get("CHATWATCH_CDP") or f"http://127.0.0.1:{PORT}"
PROFILE = HOME / "chrome-profile"
TEAMS_URL = "https://teams.cloud.microsoft/"
TEAMS_HOSTS = ("teams.cloud.microsoft", "teams.microsoft.com")
SIGN_IN_HOSTS = ("login.microsoftonline.com", "login.microsoft.com", "login.live.com")

CANDIDATES = {
    "darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
               "/Applications/Chromium.app/Contents/MacOS/Chromium",
               "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
    "linux": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"],
    "win32": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"],
}


def find_chrome():
    if os.environ.get("CHATWATCH_CHROME"):
        return os.environ["CHATWATCH_CHROME"]
    for c in CANDIDATES.get(sys.platform, CANDIDATES["linux"]):
        path = c if os.path.isabs(c) else shutil.which(c)
        if path and os.path.exists(path):
            return path
    raise SystemExit("Chrome not found; set CHATWATCH_CHROME to its executable")


def running():
    try:
        cdp.http_json(CDP_URL, "/json/version")
        return True
    except OSError:
        return False


def start(show=False):
    """Start chatwatch's Chrome unless one answers already. Returns True if started."""
    if running():
        return False
    if os.environ.get("CHATWATCH_CDP"):
        raise SystemExit(f"no Chrome answers at {CDP_URL}")
    PROFILE.mkdir(parents=True, exist_ok=True)
    port = urllib.parse.urlsplit(CDP_URL).port
    args = [find_chrome(), f"--user-data-dir={PROFILE}", f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check"]
    if not show:
        args += ["--headless=new", "--window-size=1400,900"]
    subprocess.Popen(args + [TEAMS_URL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    for _ in range(60):
        if running():
            return True
        time.sleep(0.5)
    raise SystemExit("Chrome started but DevTools did not answer")


def stop():
    """Close chatwatch's own Chrome (never one named by CHATWATCH_CDP)."""
    if os.environ.get("CHATWATCH_CDP") or not running():
        return False
    v = cdp.http_json(CDP_URL, "/json/version")
    c = cdp.CDP(v["webSocketDebuggerUrl"])
    try:
        c.call("Browser.close", timeout=5)
    except (OSError, TimeoutError, RuntimeError):
        pass
    return True


def is_teams(url):
    return urllib.parse.urlsplit(url or "").hostname in TEAMS_HOSTS


def is_sign_in(url):
    """Microsoft's sign-in pages, where the Teams tab goes when the session ends."""
    host = urllib.parse.urlsplit(url or "").hostname or ""
    return host in SIGN_IN_HOSTS or host.endswith(".microsoftonline.com")


def teams_target():
    """The Teams tab (or the tab signing in to it), opened if missing."""
    pages = [t for t in cdp.http_json(CDP_URL, "/json/list") if t.get("type") == "page"]
    for match in (is_teams, is_sign_in):
        for t in pages:
            if match(t.get("url")):
                return t
    req = urllib.request.Request(CDP_URL + "/json/new?" + urllib.parse.quote(TEAMS_URL, safe=""), method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())
