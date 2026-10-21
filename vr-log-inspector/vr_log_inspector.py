#!/usr/bin/env python3
"""
VR Log Inspector
================
Live adb logcat viewer for Android-based VR headsets (Pico 4 Ultra Enterprise,
Meta Quest 3, ...). Runs a tiny local web server and opens the UI in your browser.

Requirements:
  * Python 3.8+ (standard library only)
  * adb (Android platform-tools). If it isn't on PATH, the page offers to download
    Google's official platform-tools next to this script (or pass --adb /path/to/adb)
  * Developer mode / USB debugging enabled on the headset

Usage:
  python vr_log_inspector.py                # starts on http://127.0.0.1:8765
  python vr_log_inspector.py --port 9000 --adb "C:/platform-tools/adb.exe"
"""

import argparse
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
import zipfile
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOOL_VERSION = "1.6"
ADB = None
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # hide console windows on Windows
TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
ADB_EXE = "adb.exe" if os.name == "nt" else "adb"
PLATFORM_TOOLS_URL = os.environ.get("VRLI_ADB_URL") or (
    "https://dl.google.com/android/repository/platform-tools-latest-"
    + {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux") + ".zip")
PLATFORM_TOOLS_PAGE = "https://developer.android.com/tools/releases/platform-tools"
ADB_STATE = {"state": "missing", "message": "", "path": "", "received": 0, "total": 0}
ADB_LOCK = threading.Lock()


def adb_download_dirs():
    """Where a downloaded platform-tools may live: next to this script, or in the user's home."""
    return [TOOL_DIR, os.path.join(os.path.expanduser("~"), ".vr_log_inspector")]


def find_adb(explicit=None):
    if explicit:
        return explicit
    # an adb that is already installed comes first: mixing two adb versions makes them
    # restart each other's server (and disconnects tools like Meta Quest Developer Hub)
    found = shutil.which("adb")
    if found:
        return found
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(env)
        if root and os.path.isfile(os.path.join(root, "platform-tools", ADB_EXE)):
            return os.path.join(root, "platform-tools", ADB_EXE)
    for base in adb_download_dirs():
        cand = os.path.join(base, "platform-tools", ADB_EXE)
        if os.path.isfile(cand):
            return cand
    return None


def start_adb_download():
    with ADB_LOCK:
        if ADB_STATE["state"] == "downloading":
            return
        ADB_STATE.update(state="downloading", message="Connecting to Google…", received=0, total=0)
    threading.Thread(target=_download_adb, daemon=True).start()


def _download_adb():
    """Download Google's platform-tools zip and unpack it next to this script."""
    global ADB
    error = None
    for base in adb_download_dirs():
        tmp = os.path.join(base, "platform-tools-download.zip")
        ADB_STATE.update(received=0, total=0)
        try:
            os.makedirs(base, exist_ok=True)
            req = urllib.request.Request(PLATFORM_TOOLS_URL, headers={"User-Agent": f"VR-Log-Inspector/{TOOL_VERSION}"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
                ADB_STATE["total"] = int(resp.headers.get("Content-Length") or 0)
                ADB_STATE["message"] = "Downloading adb…"
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    ADB_STATE["received"] += len(chunk)
            ADB_STATE["message"] = "Unpacking…"
            root = os.path.realpath(base)
            with zipfile.ZipFile(tmp) as z:
                if not any(n.rstrip("/").endswith("platform-tools/" + ADB_EXE) for n in z.namelist()):
                    raise RuntimeError("the downloaded file doesn't contain adb")
                for info in z.infolist():
                    target = os.path.realpath(os.path.join(base, info.filename))
                    if not target.startswith(root + os.sep):
                        raise RuntimeError("unexpected file path inside the download")
                    z.extract(info, base)
                    mode = (info.external_attr >> 16) & 0o777
                    if mode and os.name != "nt" and not info.is_dir():
                        os.chmod(target, mode)
            os.remove(tmp)
            path = os.path.join(base, "platform-tools", ADB_EXE)
            if os.name != "nt":
                os.chmod(path, os.stat(path).st_mode | 0o755)
            ok, out = run_cmd([path, "version"], 20)
            if not ok:
                raise RuntimeError("adb was downloaded but doesn't start: " + out[:200])
            ADB = path
            ADB_STATE.update(state="ok", path=path, message="adb is ready.")
            print(f"Downloaded adb to {path}")
            return
        except PermissionError as e:   # folder not writable: try the next location
            error = e
        except Exception as e:
            error = e
            break
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    msg = str(error)
    if "CERTIFICATE_VERIFY_FAILED" in msg:
        msg += (". On macOS, run 'Install Certificates.command' from your Python folder"
                " (Applications > Python 3.x) and try again")
    ADB_STATE.update(state="error", message=f"Couldn't download adb: {msg}.")


def run_cmd(cmd, timeout=15):
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
        return res.returncode == 0, (res.stdout + res.stderr).strip()
    except (FileNotFoundError, PermissionError, OSError) as e:
        return False, str(e)
    except subprocess.TimeoutExpired:
        return False, "timed out"


def run_adb(args, timeout=15):
    if not ADB:
        return False, "adb isn't installed yet. Use the Download adb button at the top of the page."
    try:
        res = subprocess.run([ADB] + args, capture_output=True, text=True,
                             timeout=timeout, creationflags=NO_WINDOW)
        return res.returncode == 0, (res.stdout + res.stderr).strip()
    except FileNotFoundError:
        return False, f"adb not found ('{ADB}'). Install Android platform-tools or pass --adb."
    except subprocess.TimeoutExpired:
        return False, "adb timed out."


# --------------------------------------------------------------------------- buffer
class LineBuffer:
    """Ring buffer of raw log lines with monotonically increasing ids."""

    def __init__(self, maxlen):
        self.lock = threading.Lock()
        self.lines = deque(maxlen=maxlen)
        self.next = 0
        self.epoch = int(time.time() * 1000)

    def add(self, line):
        with self.lock:
            self.lines.append(line)
            self.next += 1

    def since(self, cursor, epoch, limit):
        with self.lock:
            first = self.next - len(self.lines)
            if epoch != self.epoch or cursor > self.next:
                cursor = first
            cursor = max(cursor, first)
            start = cursor - first
            out = list(itertools.islice(self.lines, start, start + limit))
            nxt = cursor + len(out)
            return out, nxt, nxt < self.next, self.epoch

    def clear(self):
        with self.lock:
            self.lines.clear()
            self.next = 0
            self.epoch = int(time.time() * 1000)


# --------------------------------------------------------------------------- capture
class Capture:
    def __init__(self, buf):
        self.buf = buf
        self.lock = threading.Lock()
        self.proc = None
        self.gen = 0
        self.running = False
        self.serial = ""
        self.message = "Not capturing. Pick a headset and press Start."
        self.procs = {}      # pid -> process name, latest snapshot from the headset
        self.procs_ver = 0

    def start(self, serial, clear_first, buffers):
        self.stop(silent=True)
        if not ADB:
            with self.lock:
                self.message = "adb isn't installed yet. Use the Download adb button at the top of the page."
            return False
        if clear_first:
            run_adb((["-s", serial] if serial else []) + ["logcat", "-c"])
        cmd = [ADB] + (["-s", serial] if serial else []) + ["logcat", "-v", "threadtime"]
        if buffers:
            cmd += ["-b", buffers]
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        except FileNotFoundError:
            with self.lock:
                self.message = f"adb not found ('{ADB}'). Install platform-tools or pass --adb."
            return False
        with self.lock:
            self.gen += 1
            gen = self.gen
            self.proc = proc
            self.running = True
            self.serial = serial
            self.message = f"Capturing from {serial or 'default device'}"
        threading.Thread(target=self._reader, args=(proc, gen), daemon=True).start()
        threading.Thread(target=self._track_procs, args=(serial, gen), daemon=True).start()
        return True

    def _track_procs(self, serial, gen):
        """Every 2 s, read which process name owns each PID, so the UI can filter by app."""
        pre = ["-s", serial] if serial else []
        while True:
            with self.lock:
                if gen != self.gen:
                    return
            ok, out = run_adb(pre + ["shell", "ps -A -o PID,NAME"], 10)
            snap = {}
            for line in out.splitlines()[1:]:
                parts = line.split(None, 1)
                if len(parts) == 2 and parts[0].isdigit():
                    snap[parts[0]] = parts[1].strip()
            if snap:
                with self.lock:
                    if gen != self.gen:
                        return
                    if snap != self.procs:
                        self.procs = snap
                        self.procs_ver += 1
            time.sleep(2)

    def procs_since(self, ver):
        with self.lock:
            return (self.procs, self.procs_ver) if ver != self.procs_ver else (None, self.procs_ver)

    def _reader(self, proc, gen):
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line:
                self.buf.add(line)
        proc.wait()
        with self.lock:
            if gen == self.gen:
                self.running = False
                self.message = (f"logcat ended (exit code {proc.returncode}). "
                                "Check the headset is connected and authorized, then press Start.")

    def stop(self, silent=False):
        with self.lock:
            self.gen += 1
            proc, self.proc = self.proc, None
            self.running = False
            if not silent:
                self.message = "Stopped."
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    def status(self):
        with self.lock:
            return {"running": self.running, "serial": self.serial, "message": self.message}


def list_apps(serial):
    pre = ["-s", serial] if serial else []
    ok, out = run_adb(pre + ["shell", "pm list packages -3 -U"], 20)
    apps = []
    for line in out.splitlines():
        m = re.match(r"package:(\S+)(?:\s+uid:(\d+))?", line.strip())
        if m:
            apps.append({"pkg": m.group(1), "uid": m.group(2) or ""})
    apps.sort(key=lambda a: a["pkg"])
    ok, out = run_adb(pre + ["shell", 'dumpsys activity activities | grep -E "ResumedActivity"'], 20)
    m = re.search(r"\s([A-Za-z][\w.]+)/[\w.$]+", out)
    return {"apps": apps, "foreground": m.group(1) if m else ""}


def list_devices():
    ok, out = run_adb(["devices", "-l"])
    if not ok:
        return {"ok": False, "error": out, "devices": []}
    devices = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        devices.append({"serial": parts[0], "state": parts[1],
                        "model": info.get("model", "").replace("_", " "),
                        "product": info.get("product", "")})
    return {"ok": True, "devices": devices}


# --------------------------------------------------------------------------- http
BUF = None
CAP = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/":
            return self._send(200, PAGE.replace("__VERSION__", TOOL_VERSION), "text/html")
        if url.path == "/api/devices":
            return self._json(list_devices())
        if url.path == "/api/adb":
            return self._json(dict(ADB_STATE, page=PLATFORM_TOOLS_PAGE))
        if url.path == "/api/apps":
            return self._json(list_apps(q.get("serial", [""])[0]))
        if url.path == "/api/logs":
            cursor = int(q.get("since", ["0"])[0] or 0)
            epoch = int(q.get("epoch", ["0"])[0] or 0)
            pver = int(q.get("pver", ["-1"])[0] or -1)
            lines, nxt, more, ep = BUF.since(cursor, epoch, 20000)
            procs, ver = CAP.procs_since(pver)
            return self._json({"lines": lines, "next": nxt, "more": more, "epoch": ep,
                               "status": CAP.status(), "procs": procs, "pver": ver})
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._body()
        if path == "/api/start":
            ok = CAP.start(body.get("serial", ""), bool(body.get("clear")),
                           body.get("buffers", ""))
            return self._json({"ok": ok, "status": CAP.status()})
        if path == "/api/adb/download":
            if ADB_STATE["state"] != "ok":
                start_adb_download()
            return self._json(dict(ADB_STATE, page=PLATFORM_TOOLS_PAGE))
        if path == "/api/stop":
            CAP.stop()
            return self._json({"ok": True, "status": CAP.status()})
        if path == "/api/clear":
            BUF.clear()
            return self._json({"ok": True})
        if path == "/api/connect":
            addr = str(body.get("addr", "")).strip()
            if not addr:
                return self._json({"ok": False, "output": "Enter the headset IP, e.g. 192.168.1.40:5555"})
            ok, out = run_adb(["connect", addr])
            return self._json({"ok": ok and "cannot" not in out.lower() and "failed" not in out.lower(),
                               "output": out})
        self._send(404, "not found", "text/plain")


def main():
    global ADB, BUF, CAP
    ap = argparse.ArgumentParser(description="Live logcat viewer for Pico / Quest headsets")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--adb", default=None, help="path to adb executable")
    ap.add_argument("--buffer", type=int, default=500000, help="max lines kept in memory")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    ADB = find_adb(args.adb)
    if ADB and (os.path.isfile(ADB) or shutil.which(ADB)):
        ADB_STATE.update(state="ok", path=ADB)
    elif ADB:
        ADB_STATE.update(state="error", message=f"The adb given with --adb doesn't exist: {ADB}")
        ADB = None
    BUF = LineBuffer(args.buffer)
    CAP = Capture(BUF)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '') else args.host}:{args.port}/"
    print(f"VR Log Inspector {TOOL_VERSION} running at {url}")
    print(f"  adb: {ADB or 'not found yet - the page will offer to download it'}")
    print("Press Ctrl+C to quit.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        CAP.stop(silent=True)
        server.server_close()


# --------------------------------------------------------------------------- UI
PAGE = r"""<!doctype html>
<html lang="en" data-theme="graphite">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>/* apply the saved theme before the first paint */try{var c=JSON.parse(localStorage.getItem("vrloginspector.v1")||"{}"),t=c.theme||"graphite";if(!c.themeVer&&t==="ludus")t="graphite";if(t==="system")t=matchMedia("(prefers-color-scheme: light)").matches?"graphite-light":"graphite";document.documentElement.dataset.theme=t;if(c.fontScale)document.documentElement.style.setProperty("--fs",c.fontScale);}catch(e){}</script>
<title>VR Log Inspector</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@500;600&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
/* ---------------------------------------------------------------- themes
   Every colour comes from these tokens. Each theme is checked so that all log text
   (every level, every tag colour) keeps at least 4.5:1 contrast on plain, shaded,
   highlighted, selected and focused rows. */
:root{
  --mono: ui-monospace,"Cascadia Mono","JetBrains Mono",Menlo,Consolas,monospace;
  --ui: "Segoe UI Variable","Segoe UI",system-ui,-apple-system,Roboto,sans-serif;
  --head: var(--ui);
  --radius: 8px; --radius-btn: 6px; --row-h: 22px; --fs: 1;
}
html[data-theme="graphite"]{
  --bg:#16181B; --header:#1D2024; --panel:#1D2024; --panel-2:#262A2F; --line:#30353B; --input-line:#4A515A;
  --text:#E6E8EB; --muted:#A4ABB5; --faint:#8E959F;
  --accent:#8FD3B0; --accent-ink:#10251B; --ok:#6FD49A;
  --c-unity:#4FCFE0; --c-xr:#B497F2; --c-system:#5D646D;
  --l-e:#FF7B72; --l-w:#E3B341; --l-d:#79C0FF; --l-v:#9AA1AB; --l-f:#FF85CB;
  --sel:rgba(143,211,176,.08); --focus:rgba(143,211,176,.13); --zebra:rgba(255,255,255,.025);
  --tag-s:55%; --tag-l:72%;
  color-scheme: dark;
}
html[data-theme="graphite-light"]{
  --bg:#F7F8FA; --header:#FFFFFF; --panel:#FFFFFF; --panel-2:#EEF0F3; --line:#D9DDE2; --input-line:#AEB5BE;
  --text:#1F2328; --muted:#535C67; --faint:#646D78;
  --accent:#1F7A57; --accent-ink:#FFFFFF; --ok:#1F7A57;
  --c-unity:#0B8494; --c-xr:#7A4DD8; --c-system:#A9B1BA;
  --l-e:#C4161C; --l-w:#855600; --l-d:#0757B8; --l-v:#5C6570; --l-f:#AD2178;
  --sel:rgba(31,122,87,.07); --focus:rgba(31,122,87,.11); --zebra:rgba(31,35,40,.03);
  --tag-s:65%; --tag-l:24%;
  color-scheme: light;
}
html[data-theme="midnight"]{
  --bg:#141827; --header:#1b2033; --panel:#1b2033; --panel-2:#22283f; --line:#2e3552; --input-line:#4a5378;
  --text:#dde2f3; --muted:#a2a9c3; --faint:#8f96b1;
  --accent:#8e9dff; --accent-ink:#10132a; --ok:#3fd38a;
  --c-unity:#4cc2ff; --c-xr:#c493ff; --c-system:#6f7894;
  --l-e:#ff8585; --l-w:#f3bb52; --l-d:#8fbaff; --l-v:#9aa1bb; --l-f:#ff85c4;
  --sel:rgba(142,157,255,.09); --focus:rgba(142,157,255,.14); --zebra:rgba(255,255,255,.025);
  --tag-s:65%; --tag-l:76%;
  color-scheme: dark;
}
html[data-theme="daylight"]{
  --bg:#eef0f7; --header:#ffffff; --panel:#ffffff; --panel-2:#f4f5fb; --line:#d9dcea; --input-line:#aeb3c8;
  --text:#1b2036; --muted:#4f5670; --faint:#5f6680;
  --accent:#4655d6; --accent-ink:#ffffff; --ok:#14804f;
  --c-unity:#0a8fd6; --c-xr:#8a4fe0; --c-system:#a3a9bf;
  --l-e:#ad1f1f; --l-w:#7d5200; --l-d:#1d52ad; --l-v:#545b74; --l-f:#a0125f;
  --sel:rgba(70,85,214,.07); --focus:rgba(70,85,214,.11); --zebra:rgba(27,32,54,.03);
  --tag-s:65%; --tag-l:23%;
  color-scheme: light;
}
html[data-theme="contrast"]{
  --bg:#000000; --header:#000000; --panel:#000000; --panel-2:#141414; --line:#5c5c5c; --input-line:#9a9a9a;
  --text:#FFFFFF; --muted:#D6D6D6; --faint:#B5B5B5;
  --accent:#FFD400; --accent-ink:#000000; --ok:#4BFF8A;
  --c-unity:#3FD0FF; --c-xr:#E08BFF; --c-system:#8A8A8A;
  --l-e:#FF7A7A; --l-w:#FFB020; --l-d:#8FD0FF; --l-v:#C4C4C4; --l-f:#FF85DD;
  --sel:rgba(255,212,0,.10); --focus:rgba(255,212,0,.15); --zebra:rgba(255,255,255,.05);
  --tag-s:90%; --tag-l:76%;
  --radius:4px; --radius-btn:4px;
  color-scheme: dark;
}
html[data-theme="ludus"]{
  --bg:#0D2B2E; --header:#0A2428; --panel:#0F3035; --panel-2:#1A3F45; --line:rgba(255,255,255,.08); --input-line:rgba(255,255,255,.24);
  --text:rgba(255,255,255,.95); --muted:rgba(255,255,255,.66); --faint:rgba(255,255,255,.58);
  --accent:#35E398; --accent-ink:#0D2B2E; --ok:#35E398;
  --c-unity:#4FC3F7; --c-xr:#B794F6; --c-system:rgba(255,255,255,.28);
  --l-e:#FF8A8A; --l-w:#F59E0B; --l-d:#7DD3FC; --l-v:rgba(255,255,255,.62); --l-f:#F79BD0;
  --sel:rgba(53,227,152,.07); --focus:rgba(53,227,152,.11); --zebra:rgba(255,255,255,.025);
  --tag-s:62%; --tag-l:76%;
  --ui:"Inter","Segoe UI",system-ui,sans-serif; --head:"Barlow Condensed","Arial Narrow",var(--ui);
  --radius:14px; --radius-btn:999px;
  color-scheme: dark;
}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--text);font:calc(13px * var(--fs))/1.4 var(--ui);display:grid;
  grid-template-rows:auto 1fr;overflow:hidden}
button,input,select{font:inherit;color:inherit}
button{background:var(--panel-2);border:1px solid var(--line);border-radius:var(--radius-btn);padding:5px 12px;cursor:pointer;transition:background .18s ease,border-color .18s ease}
button:hover{border-color:var(--accent)}
button.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);font-weight:600}
button.danger{color:var(--l-e)}
button:disabled{opacity:.45;cursor:default}
input[type=text],input[type=search],select{background:var(--panel-2);border:1px solid var(--input-line);
  border-radius:8px;padding:5px 8px;min-width:0}
input[type=checkbox]{accent-color:var(--accent)}
::placeholder{color:var(--faint)}
:focus-visible{outline:2px solid var(--accent);outline-offset:1px}

/* top bar */
header{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:8px 14px;background:var(--header);
  border-bottom:1px solid var(--line);position:relative;z-index:6}
header h1{font:600 calc(19px * var(--fs))/1 var(--head);letter-spacing:.01em;margin:0 12px 0 0;display:flex;align-items:center;gap:9px}
.lens{width:22px;height:12px;border-radius:7px;border:2px solid var(--accent);position:relative}
.lens::after{content:"";position:absolute;left:50%;top:-2px;bottom:-2px;width:2px;background:var(--header);transform:translateX(-50%)}
.grow{flex:1}
.status{display:flex;align-items:center;gap:6px;color:var(--muted);flex:0 1 300px;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#statusText{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.dot{width:8px;height:8px;border-radius:50%;background:var(--l-v);flex:none}
.dot.on{background:var(--ok);box-shadow:0 0 0 3px color-mix(in srgb, var(--ok) 25%, transparent)}
.dot.off{background:var(--l-e)}
.inline{display:flex;gap:4px;align-items:center}
label.chk{display:flex;gap:5px;align-items:center;color:var(--muted);cursor:pointer}

/* layout */
main{display:grid;grid-template-columns:min(calc(290px * var(--fs)), 40vw) minmax(0,1fr);min-height:0}
aside{background:var(--panel);border-right:1px solid var(--line);overflow:auto;padding:12px;display:flex;flex-direction:column;gap:18px}
aside h2{font:600 calc(15px * var(--fs))/1.1 var(--head);letter-spacing:.01em;margin:0 0 8px;color:var(--text)}
aside .hint{color:var(--muted);font-size:calc(11.5px * var(--fs));margin:6px 0 0}
.toggles{display:flex;flex-wrap:wrap;gap:5px}
.tg{display:flex;align-items:center;gap:6px;padding:4px 10px;border-radius:var(--radius-btn);border:1px solid var(--line);background:var(--panel-2);cursor:pointer;user-select:none}
.tg .n{color:var(--muted);font-variant-numeric:tabular-nums;font-size:calc(11px * var(--fs))}
.tg.off{opacity:.42;text-decoration:line-through}
.sw{width:9px;height:9px;border-radius:2px}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px}
.chip{display:flex;align-items:center;gap:4px;background:var(--panel-2);border:1px solid var(--line);border-radius:var(--radius-btn);padding:2px 4px 2px 9px;font-family:var(--mono);font-size:calc(12px * var(--fs));max-width:100%}
.chip .run{width:7px;height:7px;border-radius:50%;background:var(--l-v);flex:none;margin-right:2px}
.chip .run.on{background:var(--ok)}
.chip span{cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chip.off span{text-decoration:line-through;opacity:.5}
.chip.bad{border-color:var(--l-e)}
.chip button{border:0;background:none;padding:0 4px;color:var(--muted);line-height:1}
.row-in{display:flex;gap:5px}
.row-in input{flex:1}
details summary{cursor:pointer;color:var(--muted)}
details label{display:block;margin:8px 0 3px;color:var(--muted);font-size:calc(12px * var(--fs))}
details input{width:100%;font-family:var(--mono);font-size:calc(12px * var(--fs))}
.drop{border:1px dashed var(--input-line);border-radius:10px;padding:10px;text-align:center;color:var(--muted)}
.drop.over{border-color:var(--accent);color:var(--text)}

/* log pane */
section.logs{display:grid;grid-template-rows:auto auto 1fr auto;grid-template-columns:minmax(0,1fr);min-width:0;min-height:0}
main.noside{grid-template-columns:minmax(0,1fr)}
section.logs > .toolbar{grid-row:1;grid-column:1}
section.logs > .cols{grid-row:2;grid-column:1}
section.logs > .viewport{grid-row:3;grid-column:1}
section.logs > .detail{grid-row:4;grid-column:1}
/* issues + bookmarks drawer: shares rows 2-3 of the log pane, on the right */
.drawer{grid-row:2 / 4;grid-column:1;justify-self:end;align-self:stretch;width:min(calc(400px * var(--fs)), 65%);z-index:4;
  background:var(--panel);border-left:1px solid var(--line);box-shadow:-10px 0 24px rgba(0,0,0,.25);display:flex;flex-direction:column;min-height:0}
.drawer[hidden]{display:none}
.drawer .dh{display:flex;align-items:center;gap:6px;padding:8px 10px;border-bottom:1px solid var(--line)}
.drawer .tabs{display:flex;gap:4px;margin-right:auto}
.drawer .tabs button[aria-selected="true"]{border-color:var(--accent);color:var(--accent)}
.drawer .dn{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:8px 10px;border-bottom:1px solid var(--line);color:var(--muted)}
.drawer .list{overflow:auto;flex:1;min-height:0;padding:6px}
.drawer .empty2{color:var(--muted);padding:14px 8px}
.it{border:1px solid var(--line);border-radius:8px;padding:7px 9px;margin-bottom:6px;background:var(--panel-2);cursor:pointer}
.it:hover,.it:focus-visible{border-color:var(--accent)}
.it.cur{border-color:var(--accent);box-shadow:inset 3px 0 0 var(--accent)}
.it .top{display:flex;align-items:center;gap:7px;font-size:calc(12px * var(--fs))}
.it .when{font-family:var(--mono);color:var(--muted)}
.it .cnt{margin-left:auto;color:var(--muted);font-variant-numeric:tabular-nums}
.it .ttl{margin-top:4px;font-family:var(--mono);font-size:calc(12px * var(--fs));word-break:break-word}
.it .sub{margin-top:2px;color:var(--muted);font-size:calc(11.5px * var(--fs));word-break:break-word}
.it input{width:100%;margin-top:6px;font-size:calc(12px * var(--fs))}
.it .x{border:0;background:none;padding:0 4px;color:var(--muted);margin-left:auto}
.kb{display:inline-block;font-size:calc(11px * var(--fs));font-weight:600;padding:0 6px;border-radius:5px;border:1px solid currentColor;white-space:nowrap}
.k-crash,.k-native{color:var(--l-f)} .k-anr,.k-exception,.k-oom{color:var(--l-e)} .k-died,.k-lmk,.k-perf{color:var(--l-w)}
.badge{display:inline-block;min-width:1.6em;padding:0 5px;border-radius:99px;background:var(--panel-2);color:var(--muted);font-size:calc(11px * var(--fs));text-align:center;font-variant-numeric:tabular-nums;margin-left:4px}
.badge.hot{background:var(--l-e);color:var(--bg);font-weight:700}
.viewport .row.bm .tg2::before{content:"⚑ ";color:var(--accent)}
main.noside aside{display:none}
/* when large text leaves little room, drop PID first, then time; the detail panel always shows both */
.nopid .cols,.nopid .row{grid-template-columns:4px calc(26px * var(--fs)) calc(108px * var(--fs)) 0 calc(22px * var(--fs)) minmax(calc(80px * var(--fs)),calc(150px * var(--fs))) 1fr}
.notime .cols,.notime .row{grid-template-columns:4px calc(26px * var(--fs)) 0 0 calc(22px * var(--fs)) minmax(calc(70px * var(--fs)),calc(130px * var(--fs))) 1fr}
.nopid .cols .p,.nopid .row .p,.notime .cols .p,.notime .row .p,.notime .cols .t,.notime .row .t{visibility:hidden;padding:0 !important}
input[type=checkbox]{width:calc(13px * var(--fs));height:calc(13px * var(--fs))}
.row .ck i{width:calc(12px * var(--fs));height:calc(12px * var(--fs))}
.toolbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:7px 10px;border-bottom:1px solid var(--line);background:var(--panel)}
.counts{color:var(--muted);font-variant-numeric:tabular-nums;margin-right:auto}
.counts b{color:var(--text);font-weight:600}
.cols,.row{display:grid;grid-template-columns:4px calc(26px * var(--fs)) calc(108px * var(--fs)) calc(104px * var(--fs)) calc(22px * var(--fs)) minmax(calc(90px * var(--fs)),calc(170px * var(--fs))) 1fr;align-items:center}
.cols{font-size:calc(11.5px * var(--fs));color:var(--muted);padding:4px 0;border-bottom:1px solid var(--line);background:var(--panel-2)}
.cols > div{padding:0 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.viewport{overflow:auto;position:relative;font-family:var(--mono);font-size:calc(12px * var(--fs));outline:none}
.spacer{position:relative;width:100%}
.rows{position:absolute;left:0;right:0;top:0;will-change:transform}
.row{height:var(--row-h);white-space:nowrap;cursor:default}
.row > div{overflow:hidden;text-overflow:ellipsis;padding:0 6px}
.row .rail{padding:0;height:100%}
/* optional extras, switched on from Appearance */
.zebra .row.odd{background:var(--zebra)}
.tint .row.lvl-W{background:color-mix(in srgb, var(--l-w) 7%, transparent)}
.tint .row.lvl-E{background:color-mix(in srgb, var(--l-e) 9%, transparent)}
.tint .row.lvl-F{background:color-mix(in srgb, var(--l-f) 14%, transparent)}
.colortags .row .tg2{color:hsl(var(--h) var(--tag-s) var(--tag-l))}
.viewport .row:hover{background:var(--panel-2)}
.viewport .row.sel{background:var(--sel)}
.viewport .row.focus{background:var(--focus);box-shadow:inset 0 0 0 1px color-mix(in srgb, var(--accent) 60%, transparent)}
.viewport .row.anchor{box-shadow:inset 0 0 0 2px var(--accent)}
.row.anchor .rail{background:var(--accent)}
.row .ck{display:flex;justify-content:center}
.row .ck i{width:12px;height:12px;border:1.5px solid var(--muted);border-radius:3px;display:block;cursor:pointer}
.row.sel .ck i{background:var(--accent);border-color:var(--accent)}
.row .t,.row .p{color:var(--muted)}
.row .lv{font-weight:700;text-align:center;padding:0 !important;margin:0 2px;border-radius:4px;line-height:calc(16px * var(--fs));font-size:calc(11px * var(--fs))}
.lvl-E .lv{background:color-mix(in srgb, var(--l-e) 20%, transparent)}
.lvl-W .lv{background:color-mix(in srgb, var(--l-w) 20%, transparent)}
.row.lvl-F .lv{background:var(--l-f);color:var(--bg) !important}
.row .tg2{color:var(--muted)}
.cat-unity .rail{background:var(--c-unity)} .cat-xr .rail{background:var(--c-xr)} .cat-system .rail{background:var(--c-system)}
.lvl-E .lv,.lvl-E .m{color:var(--l-e)} .lvl-W .lv,.lvl-W .m{color:var(--l-w)}
.lvl-D .lv{color:var(--l-d)} .lvl-V .lv,.lvl-V .m{color:var(--l-v)}
.lvl-F .lv,.lvl-F .m{color:var(--l-f);font-weight:600}
mark{background:color-mix(in srgb, var(--accent) 35%, transparent);color:inherit;border-radius:2px}
.empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;text-align:center;color:var(--muted);font-family:var(--ui);padding:20px;pointer-events:none}
.empty p{max-width:420px}

.detail{border-top:1px solid var(--line);background:var(--panel);max-height:34vh;display:flex;flex-direction:column;min-height:0}
.detail .bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:6px 10px;border-bottom:1px solid var(--line)}
.detail .meta{color:var(--muted);font-family:var(--mono);font-size:calc(12px * var(--fs));margin-right:auto;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.detail pre{margin:0;padding:8px 10px;overflow:auto;font:calc(12px * var(--fs))/1.5 var(--mono);white-space:pre-wrap;word-break:break-word}
.detail.hidden{display:none}

.toast{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--text);color:var(--bg);padding:8px 16px;border-radius:var(--radius-btn);opacity:0;transition:opacity .2s;pointer-events:none;z-index:9}
.toast.show{opacity:1}
.adbbar{flex-basis:100%;display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;padding:10px 12px;margin-top:4px;
  border:1px solid color-mix(in srgb, var(--accent) 45%, transparent);background:color-mix(in srgb, var(--accent) 9%, var(--panel));border-radius:var(--radius)}
.adbbar[hidden]{display:none}
.adbbar.err{border-color:var(--l-e);background:color-mix(in srgb, var(--l-e) 9%, var(--panel))}
.adbbar .txt{flex:1 1 360px;min-width:0}
.adbbar .txt small{display:block;color:var(--muted);margin-top:2px}
.adbbar a{color:var(--accent)}
.adbbar.err a{color:var(--l-e)}
.prog{flex:1 1 200px;height:8px;border-radius:99px;background:var(--panel-2);overflow:hidden;min-width:120px}
.prog i{display:block;height:100%;width:0;background:var(--accent);transition:width .25s ease}
@media (prefers-reduced-motion: reduce){.prog i{transition:none}}
.appear{position:absolute;right:12px;top:calc(100% + 6px);width:calc(340px * var(--fs));max-width:calc(100vw - 24px);background:var(--panel);border:1px solid var(--line);
  border-radius:var(--radius);padding:14px;box-shadow:0 12px 32px rgba(0,0,0,.35);z-index:20}
.appear[hidden]{display:none}
.appear h3{font:600 calc(17px * var(--fs))/1.1 var(--head);margin:0 0 10px}
.appear h4{font-size:calc(12px * var(--fs));color:var(--muted);font-weight:500;margin:14px 0 6px}
.themes{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.th{padding:0;border-radius:10px;overflow:hidden;text-align:left;background:var(--panel-2);border:2px solid var(--line)}
.th[aria-checked="true"]{border-color:var(--accent)}
.th .pv{height:46px;display:grid;grid-template-columns:26% 1fr}
.th .pv i{display:block}
.th .pv b{display:flex;flex-direction:column;justify-content:center;gap:4px;padding:0 7px}
.th .pv b span{display:block;height:3px;border-radius:2px}
.th .nm{display:block;font-size:calc(11.5px * var(--fs));padding:5px 7px;color:var(--text)}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:var(--radius-btn);overflow:hidden}
.seg button{border:0;border-radius:0;background:none;padding:4px 12px}
.seg button[aria-pressed="true"]{background:var(--accent);color:var(--accent-ink)}
.appear label.chk{margin:6px 0}
@media (prefers-reduced-motion: reduce){.toast{transition:none}}
@media (max-width: 860px){
  main{grid-template-columns:1fr;grid-template-rows:auto 1fr}
  aside{max-height:38vh;border-right:0;border-bottom:1px solid var(--line)}
  .cols,.row{grid-template-columns:4px calc(26px * var(--fs)) calc(84px * var(--fs)) 0 calc(22px * var(--fs)) calc(110px * var(--fs)) 1fr}
  .cols .p,.row .p{visibility:hidden;padding:0 !important}
}
.sizer{display:flex;align-items:center;gap:6px}
.sizer output{min-width:4.2em;text-align:center;font-variant-numeric:tabular-nums}
.fsq{display:flex;gap:2px}
.fsq button{padding:4px 9px;font-weight:600}
.fsq button:first-child{border-top-right-radius:0;border-bottom-right-radius:0}
.fsq button:last-child{border-top-left-radius:0;border-bottom-left-radius:0}
</style>
</head>
<body>
<header>
  <h1><span class="lens" aria-hidden="true"></span>VR Log Inspector</h1>
  <div class="inline">
    <select id="device" title="Headset" style="max-width:240px"><option value="">No headset found</option></select>
    <button id="refresh" title="Rescan connected headsets">Rescan</button>
  </div>
  <div class="inline">
    <input id="wifiAddr" type="text" placeholder="IP:5555 for Wi-Fi adb" style="width:170px">
    <button id="connect">Connect</button>
  </div>
  <select id="buffers" title="Which logcat buffers to read">
    <option value="main,system,crash">main + system + crash</option>
    <option value="all">all buffers</option>
    <option value="">device default</option>
  </select>
  <label class="chk" title="Runs 'adb logcat -c' before capturing so you only see new logs"><input type="checkbox" id="clearFirst"> Clear device log first</label>
  <button id="start" class="primary">Start</button>
  <button id="stop" disabled>Stop</button>
  <span class="grow"></span>
  <div class="status" id="status"><span class="dot" id="dot"></span><span id="statusText">Connecting to local server…</span></div>
  <div class="fsq" role="group" aria-label="Text size">
    <button id="fsDownQ" title="Smaller text" aria-label="Smaller text">A−</button><button id="fsUpQ" title="Larger text" aria-label="Larger text">A+</button>
  </div>
  <button id="appearBtn" aria-haspopup="dialog" aria-expanded="false" title="Theme, text size and display options">Appearance</button>
  <div class="appear" id="appear" role="dialog" aria-label="Appearance" hidden>
    <h3>Appearance</h3>
    <div class="themes" id="themes" role="radiogroup" aria-label="Theme"></div>
    <h4>Text size</h4>
    <div class="sizer">
      <button id="fsDown" aria-label="Smaller text">A−</button>
      <output id="fsVal" aria-live="polite">100%</output>
      <button id="fsUp" aria-label="Larger text">A+</button>
      <button id="fsReset">Reset</button>
    </div>
    <h4>Log list</h4>
    <label class="chk"><input type="checkbox" id="optTags"> Give each tag its own colour</label>
    <label class="chk"><input type="checkbox" id="optTint"> Highlight warning and error rows</label>
    <label class="chk"><input type="checkbox" id="optZebra"> Alternate row shading</label>
    <h4>Row height</h4>
    <div class="seg" id="density">
      <button data-d="compact" aria-pressed="false">Compact</button><button data-d="normal" aria-pressed="false">Normal</button><button data-d="comfy" aria-pressed="false">Roomy</button>
    </div>
    <p style="margin:14px 0 0;color:var(--muted);font-size:calc(11.5px * var(--fs))">VR Log Inspector __VERSION__</p>
  </div>
  <div class="adbbar" id="adbBar" role="status" aria-live="polite" hidden>
    <div class="txt" id="adbTxt"></div>
    <div class="prog" id="adbProg" hidden><i id="adbFill"></i></div>
    <button class="primary" id="adbGet">Download adb</button>
  </div>
</header>

<main id="main">
<aside id="side">
  <div>
    <h2>Apps</h2>
    <label class="chk"><input type="checkbox" id="appsOn"> Only show logs from these apps</label>
    <div class="row-in" style="margin-top:7px">
      <input id="appIn" type="text" list="appList" placeholder="com.company.app">
      <button id="appAdd">Add</button>
    </div>
    <datalist id="appList"></datalist>
    <button id="appOpen" style="margin-top:6px;width:100%">Add the app open in the headset</button>
    <div class="chips" id="appChips"></div>
    <label class="chk" style="margin-top:8px" title="If a watched app launches another one (e.g. a platform opening a module), it is added automatically"><input type="checkbox" id="appFollow"> Also watch apps they launch</label>
    <label class="chk" style="margin-top:4px" title="Lines written by the system about these apps: process start and death, crashes reported by the system, ANRs"><input type="checkbox" id="appMention"> Include system lines that mention them</label>
    <p class="hint">The green dot means the app is running. Click a name to pause it.</p>
  </div>

  <div>
    <h2>Search</h2>
    <input id="search" type="search" placeholder='e.g. NullReference  tag:Unity  -shader' style="width:100%">
    <p class="hint">Words must all match. <code>tag:Name</code> matches a tag (<code>*</code> wildcard), <code>pid:1234</code> a process, <code>-word</code> excludes, <code>/regex/</code> works too.</p>
  </div>

  <div>
    <h2>Source</h2>
    <div class="toggles" id="cats"></div>
  </div>

  <div>
    <h2>Level</h2>
    <div class="toggles" id="levels"></div>
    <p class="hint">Alt-click a source or level to show only that one.</p>
  </div>

  <div>
    <h2>Ignore lines containing</h2>
    <div class="row-in">
      <input id="ignoreIn" type="text" placeholder="keyword, tag:Name or /regex/">
      <button id="ignoreAdd">Add</button>
    </div>
    <div class="chips" id="chips"></div>
    <label class="chk" style="margin-top:8px"><input type="checkbox" id="ignoreOn" checked> Apply ignore list</label>
    <p class="hint">Click a keyword to pause it without deleting. The list is saved in this browser.</p>
  </div>

  <details>
    <summary>Which tags count as Unity / XR runtime</summary>
    <label for="unityTags">Unity tags</label>
    <input id="unityTags" type="text">
    <label for="xrTags">XR runtime tags (Meta, Pico, OpenXR)</label>
    <input id="xrTags" type="text">
    <p class="hint">Comma separated, case-insensitive, <code>*</code> wildcard. Everything else is System.</p>
    <button id="rulesReset" style="margin-top:6px">Reset to defaults</button>
  </details>

  <div>
    <h2>Open a saved log</h2>
    <div class="drop" id="drop">Drop a .txt / .log file here or <button id="pick" type="button">choose a file</button></div>
    <input type="file" id="file" accept=".txt,.log,.logcat,text/plain" hidden multiple>
  </div>
</aside>

<section class="logs">
  <div class="toolbar">
    <div class="counts" id="counts"></div>
    <button id="backSearch" hidden title="Restore the search you had before jumping"></button>
    <button id="selAll" title="Select every line currently shown (Ctrl+A)">Select shown</button>
    <button id="selNone" title="Esc">Clear selection</button>
    <select id="expScope" title="What to export">
      <option value="selected">Selected lines</option>
      <option value="filtered">All shown lines</option>
      <option value="bookmarked">Bookmarked lines</option>
      <option value="all">Everything captured</option>
    </select>
    <select id="expFmt" title="File format">
      <option value="log">.log (raw logcat)</option>
      <option value="csv">.csv</option>
      <option value="json">.json</option>
    </select>
    <button id="export" class="primary">Export</button>
    <label class="chk" title="Keep the newest line in view"><input type="checkbox" id="follow" checked> Follow</label>
    <button id="clearLogs" class="danger" title="Clear the lines in this viewer">Clear</button>
    <button id="sideBtn" aria-controls="side" aria-expanded="true" title="Show or hide the filter panel">Hide filters</button>
    <button id="issuesBtn" aria-controls="drawer" title="Crashes, ANRs, exceptions and other problems found in the log (F8 next, Shift+F8 previous)">Issues<span class="badge" id="issuesCnt">0</span></button>
    <button id="bmBtn" aria-controls="drawer" title="Lines you bookmarked (B to bookmark, F2 next, Shift+F2 previous)">Bookmarks<span class="badge" id="bmCnt">0</span></button>
  </div>
  <div class="cols" aria-hidden="true">
    <div></div><div></div><div class="t">Time</div><div class="p">PID / TID</div><div>L</div><div>Tag</div><div>Message</div>
  </div>
  <aside class="drawer" id="drawer" hidden aria-label="Issues and bookmarks">
    <div class="dh">
      <div class="tabs" role="tablist">
        <button role="tab" id="tabIssues" aria-selected="true">Issues</button>
        <button role="tab" id="tabBm" aria-selected="false">Bookmarks</button>
      </div>
      <button id="dwPrev" title="Previous (Shift+F8 / Shift+F2)">↑</button>
      <button id="dwNext" title="Next (F8 / F2)">↓</button>
      <button id="dwClose" title="Close">✕</button>
    </div>
    <div class="dn" id="dwNote"></div>
    <div class="list" id="dwList"></div>
  </aside>
  <div class="viewport" id="vp" tabindex="0">
    <div class="spacer" id="spacer"><div class="rows" id="rows"></div></div>
    <div class="empty" id="empty"><p>Connect your headset over USB (or Wi-Fi adb), pick it above and press Start. You can also drop a saved logcat file on the left.</p></div>
  </div>
  <div class="detail hidden" id="detail">
    <div class="bar">
      <span class="meta" id="dMeta"></span>
      <button id="dContext" class="primary" title="Clear the search and show this line among everything around it (Enter or double-click a line)">Show in context</button>
      <button id="dBm" title="Bookmark this line (B)">Bookmark</button>
      <button id="dCopy">Copy line</button>
      <button id="dOnlyTag">Show only this tag</button>
      <button id="dIgnoreTag">Ignore this tag</button>
      <button id="dIgnoreText">Ignore similar text…</button>
      <button id="dClose" title="Close">✕</button>
    </div>
    <pre id="dMsg"></pre>
  </div>
</section>
</main>
<div class="toast" id="toast"></div>

<script>
"use strict";
const $ = id => document.getElementById(id);
let ROW_H = 22;
const OVERSCAN = 20, MAX_LINES = 400000;
const LEVELS = ["V","D","I","W","E","F"];
const LEVEL_NAMES = {V:"Verbose",D:"Debug",I:"Info",W:"Warn",E:"Error",F:"Fatal"};
const CATS = [["unity","Unity","var(--c-unity)"],["xr","XR runtime","var(--c-xr)"],["system","System","var(--c-system)"]];
const DEFAULT_UNITY = "Unity, UnityMain, UnityGfx*, IL2CPP, Mono, CRASH_Unity*";
const DEFAULT_XR = "VrApi, VrRuntime*, OVR*, Oculus*, XrRuntime*, OpenXR*, Pxr*, Pico*, PVR*, PxrService*, XRServer*";
const STORE = "vrloginspector.v1";

// ------------------------------------------------------------------ settings
const STORED = (()=>{ try { return JSON.parse(localStorage.getItem(STORE)||"{}"); } catch { return {}; } })();
const settings = Object.assign({
  ignore: [],
  ignoreOn: true,
  unityTags: DEFAULT_UNITY, xrTags: DEFAULT_XR,
  levels: LEVELS.slice(), cats: CATS.map(c=>c[0]),
  buffers: "main,system,crash", clearFirst: false, serial: "", wifi: "",
  expScope: "selected", expFmt: "log",
  apps: [], appsOn: false, appFollow: true, appMention: true,
  theme: "graphite", colorTags: true, tintRows: true, zebra: false, density: "normal", fontScale: 1, sideHidden: false, issuesAppOnly: true, themeVer: 2
}, STORED);
// one-time move from the old default (Ludus) to Graphite; a theme picked later is kept
if(!STORED.themeVer && (STORED.theme === "ludus" || STORED.theme === "ludus-light")) settings.theme = "graphite";
function save(){ try { localStorage.setItem(STORE, JSON.stringify(settings)); } catch {} }

// ------------------------------------------------------------------ state
let entries = [], filtered = [], seq = 0;
const selected = new Set();
let focusId = null, anchorIdx = null;
let ignoredCount = 0;
const lvlCount = {}, catCount = {};
let unityRx = [], xrRx = [], catCache = new Map();
let searchTests = [], searchWords = [], ignoreTests = [];
let cursor = 0, epoch = 0;
let contextId = null, prevSearch = null;
const pidName = new Map();          // pid -> process name (kept after the process dies)
let alive = new Set(), pver = -1;   // pids running in the latest snapshot
const uidPkg = new Map();           // app uid -> package, to follow launches
let watchedSet = new Set(), mentionRx = null;

// ------------------------------------------------------------------ parsing
const RE_THREADTIME = /^((?:\d{4}-)?\d\d-\d\d)\s+(\d\d:\d\d:\d\d\.\d+)\s+(\d+)\s+(\d+)\s+([VDIWEFAS])\s+(.*?)\s*: ?(.*)$/;
const RE_BRIEF = /^([VDIWEFA])\/(.*?)\(\s*(\d+)\):\s?(.*)$/;
function wildToRx(p){ return new RegExp("^" + p.replace(/[.+?^${}()|[\]\\]/g,"\\$&").replace(/\*/g,".*") + "$", "i"); }
function compileTags(s){ return s.split(",").map(x=>x.trim()).filter(Boolean).map(wildToRx); }
function categorize(tag){
  if(!tag) return "system";
  let c = catCache.get(tag);
  if(c) return c;
  c = unityRx.some(r=>r.test(tag)) ? "unity" : xrRx.some(r=>r.test(tag)) ? "xr" : "system";
  catCache.set(tag, c);
  return c;
}
function parseLine(raw){
  let m = RE_THREADTIME.exec(raw), e;
  if(m) e = {d:m[1], t:m[2], pid:m[3], tid:m[4], lvl:m[5], tag:m[6].trim(), msg:m[7]};
  else if((m = RE_BRIEF.exec(raw))) e = {d:"", t:"", pid:m[3], tid:"", lvl:m[1], tag:m[2].trim(), msg:m[4]};
  else e = {d:"", t:"", pid:"", tid:"", lvl:"V", tag:"", msg:raw};
  if(e.lvl === "A") e.lvl = "F";
  if(e.lvl === "S") e.lvl = "V";
  e.id = seq++; e.raw = raw;
  e.lc = (e.tag + " " + e.msg).toLowerCase();
  e.cat = categorize(e.tag);
  return e;
}

// ------------------------------------------------------------------ matching
// One token: plain text, tag:Name (wildcards), pid:123, /regex/flags
function compileToken(tok){
  tok = tok.trim();
  if(!tok) return null;
  const rm = /^\/(.+)\/([gimsuy]*)$/.exec(tok);
  if(rm){ const rx = new RegExp(rm[1], rm[2].replace("g","") || "i"); return e => rx.test(e.tag + " " + e.msg); }
  const fm = /^(tag|pid):(.+)$/i.exec(tok);
  if(fm){
    const v = fm[2].replace(/^"|"$/g,"");
    if(fm[1].toLowerCase() === "tag"){ const rx = wildToRx(v); return e => rx.test(e.tag); }
    return e => e.pid === v || e.tid === v;
  }
  const lc = tok.replace(/^"|"$/g,"").toLowerCase();
  return e => e.lc.includes(lc);
}
function compileSearch(){
  const q = $("search").value.trim();
  const toks = q.match(/-?(?:\w+:)?(?:"[^"]*"|\/(?:\\.|[^\/])+\/[a-z]*|\S+)/g) || [];
  searchTests = []; searchWords = [];
  for(let tok of toks){
    let neg = false;
    if(tok.startsWith("-") && tok.length > 1){ neg = true; tok = tok.slice(1); }
    let fn; try { fn = compileToken(tok); } catch { fn = null; }
    if(!fn) continue;
    searchTests.push(neg ? (e => !fn(e)) : fn);
    if(!neg && !/^(tag|pid):/i.test(tok) && !tok.startsWith("/")) searchWords.push(tok.replace(/^"|"$/g,""));
  }
}
function compileIgnore(){
  ignoreTests = [];
  settings.ignore.forEach(k => {
    k.bad = false;
    if(!k.on) return;
    try { const fn = compileToken(k.text); if(fn) ignoreTests.push(fn); } catch { k.bad = true; }
  });
}
function compileRules(){ unityRx = compileTags(settings.unityTags); xrRx = compileTags(settings.xrTags); catCache = new Map(); }

const levelOn = new Set(settings.levels), catOn = new Set(settings.cats);
// ------------------------------------------------------------------ app filter
const RE_STARTPROC = /^Start proc (\d+):([^\s\/]+)\//;
const RE_LAUNCH = /\bSTART u\d+ \{.*?\bcmp=([\w.]+)\/.*?\}.*?\bfrom uid (\d+)/;
const RE_PKG = /^[A-Za-z]\w*(\.\w+)+$/;
const basePkg = name => name ? name.split(":")[0] : "";
const pkgOfPid = pid => basePkg(pidName.get(pid));
function compileApps(){
  watchedSet = new Set(settings.apps.filter(a => a.on).map(a => a.pkg));
  mentionRx = watchedSet.size ? new RegExp("(^|[^\\w.])(" + [...watchedSet].map(p => p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")(?![\\w])") : null;
}
const appActive = () => settings.appsOn && watchedSet.size > 0;
function appMatch(e){
  if(!appActive()) return true;
  if(e.pid && watchedSet.has(pkgOfPid(e.pid))) return true;
  return settings.appMention && mentionRx !== null && mentionRx.test(e.msg);
}
// Learn from the log itself: new processes, and apps launched by a watched app
function learn(e){
  if(e.tag !== "ActivityManager" && e.tag !== "ActivityTaskManager") return false;
  let m = RE_STARTPROC.exec(e.msg);
  if(m){ pidName.set(m[1], m[2]); alive.add(m[1]); return false; }
  if(settings.appFollow && (m = RE_LAUNCH.exec(e.msg))){
    const from = uidPkg.get(m[2]), target = m[1];
    if(from && watchedSet.has(from) && target !== from && !settings.apps.some(a => a.pkg === target)){
      addApp(target, from); return true;
    }
  }
  return false;
}
function addApp(pkg, via){
  pkg = (pkg || "").trim();
  if(!RE_PKG.test(pkg)){ toast("That doesn't look like a package name, e.g. com.company.app"); return false; }
  if(settings.apps.some(a => a.pkg === pkg)){ toast(pkg + " is already in the list"); return false; }
  settings.apps.push({pkg, on: true, via: via || ""});
  if(!via && !settings.appsOn){ settings.appsOn = true; $("appsOn").checked = true; }
  save(); compileApps(); renderApps();
  toast(via ? `Now also watching ${pkg} (launched by ${via})` : `Showing logs from ${pkg}`);
  return true;
}
let appsKey = "";
function renderApps(){
  const running = new Set([...alive].map(p => pkgOfPid(p)));
  const key = JSON.stringify(settings.apps) + [...running].filter(p => watchedSet.has(p) || settings.apps.some(a => a.pkg === p)).sort();
  if(key === appsKey) return;
  appsKey = key;
  $("appChips").innerHTML = settings.apps.map((a, i) => {
    const on = running.has(a.pkg);
    return `<div class="chip${a.on ? "" : " off"}" title="${a.via ? "Added automatically: launched by " + esc(a.via) : "Click to pause / resume"}">` +
      `<i class="run${on ? " on" : ""}" title="${on ? "Running" : "Not running"}"></i><span data-i="${i}">${esc(a.pkg)}</span>` +
      `<button data-del="${i}" aria-label="Remove ${esc(a.pkg)}">✕</button></div>`;
  }).join("") || `<span class="hint" style="margin:0">No apps selected: every process is shown.</span>`;
}
function mergeProcs(snap){
  let changed = false;
  alive = new Set(Object.keys(snap));
  for(const [pid, name] of Object.entries(snap)){
    if(pidName.get(pid) !== name){ pidName.set(pid, name); changed = true; }
  }
  renderApps();
  if(changed && appActive()) rebuild(true);
}
async function loadApps(){
  const serial = $("device").value; if(!serial) return null;
  try {
    const j = await api("/api/apps?serial=" + encodeURIComponent(serial));
    uidPkg.clear();
    j.apps.forEach(a => { if(a.uid) uidPkg.set(a.uid, a.pkg); });
    $("appList").innerHTML = j.apps.map(a => `<option value="${esc(a.pkg)}">`).join("");
    return j;
  } catch { return null; }
}

function isIgnored(e){ return settings.ignoreOn && ignoreTests.length && ignoreTests.some(f => f(e)); }
function passes(e){
  if(!appMatch(e)) return false;
  if(!levelOn.has(e.lvl) || !catOn.has(e.cat)) return false;
  for(const f of searchTests) if(!f(e)) return false;
  return true;
}
function accept(e){
  if(isIgnored(e)){ ignoredCount++; return false; }
  return passes(e);
}
function rebuild(keepScroll){
  ignoredCount = 0;
  try { drawerDirty = true; updateMarkCounts(); renderDrawer(); } catch {}   // the visible issues depend on the filters
  filtered = entries.filter(accept);
  if(focusId !== null && !filtered.some(e => e.id === focusId)) setFocus(null);
  anchorIdx = null;
  if(!keepScroll && settings.follow !== false && $("follow").checked) stickBottom = true;
  scheduleRender(); updateCounts();
}
function recount(){
  LEVELS.forEach(l => lvlCount[l] = 0); CATS.forEach(c => catCount[c[0]] = 0);
  for(const e of entries){ lvlCount[e.lvl]++; catCount[e.cat]++; }
}

// ------------------------------------------------------------------ adding lines
function addLines(lines){
  const vp = $("vp");
  const atBottom = vp.scrollTop + vp.clientHeight >= vp.scrollHeight - ROW_H * 2;
  let launched = false;
  for(const raw of lines){
    const e = parseLine(raw);
    if(learn(e)) launched = true;
    entries.push(e);
    detectIssue(e);
    lvlCount[e.lvl] = (lvlCount[e.lvl]||0) + 1;
    catCount[e.cat] = (catCount[e.cat]||0) + 1;
    if(accept(e)) filtered.push(e);
  }
  if(entries.length > MAX_LINES){
    entries = entries.slice(entries.length - Math.floor(MAX_LINES * 0.9));
    pruneMarks(); recount(); rebuild(true);
  } else if(launched) rebuild(true);   // a newly watched app may have logged before it was launched
  if($("follow").checked && (atBottom || stickBottom)) stickBottom = true;
  $("empty").style.display = entries.length ? "none" : "";
  scheduleRender(); scheduleCounts();
  if(drawerDirty){ updateMarkCounts(); renderDrawer(); }
}

// ------------------------------------------------------------------ rendering
let renderQueued = false, stickBottom = false;
function scheduleRender(){ if(!renderQueued){ renderQueued = true; requestAnimationFrame(render); } }
function esc(s){ return s.replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
let hlRx = null;
function highlight(s){
  const h = esc(s);
  return hlRx ? h.replace(hlRx, "<mark>$&</mark>") : h;
}
function render(){
  renderQueued = false;
  const vp = $("vp");
  $("spacer").style.height = (filtered.length * ROW_H) + "px";
  if(stickBottom){ vp.scrollTop = vp.scrollHeight; stickBottom = false; }
  const top = vp.scrollTop, h = vp.clientHeight;
  const start = Math.max(0, Math.floor(top / ROW_H) - OVERSCAN);
  const end = Math.min(filtered.length, Math.ceil((top + h) / ROW_H) + OVERSCAN);
  let html = "";
  for(let i = start; i < end; i++){
    const e = filtered[i];
    const cls = "row lvl-" + e.lvl + " cat-" + e.cat + (i & 1 ? " odd" : "") + (selected.has(e.id) ? " sel" : "") + (e.id === focusId ? " focus" : "") + (e.id === contextId ? " anchor" : "") + (bookmarks.has(e.id) ? " bm" : "");
    html += `<div class="${cls}" data-i="${i}"><div class="rail"></div><div class="ck"><i></i></div>` +
      `<div class="t">${e.t}</div><div class="p">${e.pid}${e.tid ? " / " + e.tid : ""}</div>` +
      `<div class="lv">${e.lvl}</div><div class="tg2" style="--h:${tagHue(e.tag)}" title="${esc(e.tag)}">${esc(e.tag)}</div>` +
      `<div class="m">${highlight(e.msg)}</div></div>`;
  }
  const rows = $("rows");
  rows.style.transform = `translateY(${start * ROW_H}px)`;
  rows.innerHTML = html;
}
let countsQueued = false;
function scheduleCounts(){ if(!countsQueued){ countsQueued = true; setTimeout(()=>{ countsQueued = false; updateCounts(); }, 250); } }
const fmt = n => n.toLocaleString();
function updateCounts(){
  $("counts").innerHTML = `<b>${fmt(filtered.length)}</b> shown of ${fmt(entries.length)} · ${fmt(ignoredCount)} ignored · <b>${fmt(selected.size)}</b> selected${appActive() ? ` · <span title="${esc([...watchedSet].join(", "))}">${watchedSet.size} app${watchedSet.size > 1 ? "s" : ""}</span>` : ""}`;
  document.querySelectorAll("#levels .tg").forEach(el => el.querySelector(".n").textContent = fmt(lvlCount[el.dataset.k]||0));
  document.querySelectorAll("#cats .tg").forEach(el => el.querySelector(".n").textContent = fmt(catCount[el.dataset.k]||0));
}

// ------------------------------------------------------------------ sidebar UI
function buildToggles(){
  $("cats").innerHTML = CATS.map(([k,name,col]) =>
    `<div class="tg${catOn.has(k)?"":" off"}" data-k="${k}" role="button" tabindex="0"><span class="sw" style="background:${col}"></span>${name}<span class="n">0</span></div>`).join("");
  $("levels").innerHTML = LEVELS.map(l =>
    `<div class="tg${levelOn.has(l)?"":" off"}" data-k="${l}" role="button" tabindex="0" title="${LEVEL_NAMES[l]}"><span class="lvl-${l}"><b class="lv" style="color:var(--l-${l.toLowerCase()})">${l}</b></span><span class="n">0</span></div>`).join("");
}
function toggleIn(set, key, el, prop){
  if(set.has(key)) set.delete(key); else set.add(key);
  el.classList.toggle("off", !set.has(key));
  settings[prop] = [...set]; save(); rebuild();
}
function onToggle(container, set, prop){
  const h = ev => {
    const el = ev.target.closest(".tg"); if(!el) return;
    if(ev.type === "keydown" && ev.key !== "Enter" && ev.key !== " ") return;
    ev.preventDefault();
    if(ev.altKey){ // alt-click: show only this one
      set.clear(); set.add(el.dataset.k);
      container.querySelectorAll(".tg").forEach(t => t.classList.toggle("off", !set.has(t.dataset.k)));
      settings[prop] = [...set]; save(); rebuild(); return;
    }
    toggleIn(set, el.dataset.k, el, prop);
  };
  container.addEventListener("click", h); container.addEventListener("keydown", h);
}
function renderChips(){
  $("chips").innerHTML = settings.ignore.map((k,i) =>
    `<div class="chip${k.on?"":" off"}${k.bad?" bad":""}" title="${k.bad?"Invalid regex":"Click to pause / resume"}"><span data-i="${i}">${esc(k.text)}</span><button data-del="${i}" aria-label="Remove">✕</button></div>`).join("");
}
function addIgnore(text){
  text = text.trim(); if(!text) return;
  if(!settings.ignore.some(k => k.text === text)) settings.ignore.push({text, on:true});
  save(); compileIgnore(); renderChips(); rebuild(true);
  toast(`Ignoring "${text}"`);
}

// ------------------------------------------------------------------ selection & detail
function setFocus(id){
  focusId = id;
  syncBmButton();
  if(!$("drawer").hidden){ drawerDirty = true; renderDrawer(); }
  const e = id === null ? null : entries.find(x => x.id === id);
  if(!e){ $("detail").classList.add("hidden"); return; }
  $("detail").classList.remove("hidden");
  const catName = CATS.find(c => c[0] === e.cat)[1];
  $("dMeta").textContent = [e.d && (e.d + " " + e.t), e.pid && ("pid " + e.pid + (e.tid ? " tid " + e.tid : "") + (pidName.get(e.pid) ? " (" + pidName.get(e.pid) + ")" : "")), LEVEL_NAMES[e.lvl], "tag " + (e.tag || "(none)"), "source " + catName].filter(Boolean).join("   ");
  $("dMsg").textContent = e.msg;
  $("dOnlyTag").disabled = $("dIgnoreTag").disabled = !e.tag;
}
function selectRange(a, b){
  const [s, t] = a < b ? [a, b] : [b, a];
  for(let i = s; i <= t; i++) selected.add(filtered[i].id);
}
$("rows").addEventListener("click", ev => {
  const row = ev.target.closest(".row"); if(!row) return;
  const i = +row.dataset.i, e = filtered[i];
  if(ev.shiftKey && anchorIdx !== null){ selectRange(anchorIdx, i); }
  else if(ev.target.closest(".ck") || ev.ctrlKey || ev.metaKey){
    if(selected.has(e.id)) selected.delete(e.id); else selected.add(e.id);
    anchorIdx = i;
  } else { anchorIdx = i; }
  setFocus(e.id); scheduleRender(); updateCounts();
});
$("vp").addEventListener("scroll", () => {
  const vp = $("vp");
  const atBottom = vp.scrollTop + vp.clientHeight >= vp.scrollHeight - ROW_H * 2;
  if(!atBottom && $("follow").checked && !stickBottom) $("follow").checked = false;
  scheduleRender();
});
$("follow").addEventListener("change", ev => { if(ev.target.checked){ stickBottom = true; scheduleRender(); } });
window.addEventListener("resize", scheduleRender);

document.addEventListener("keydown", ev => {
  const typing = /^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName);
  if(typing) return;
  const mod = ev.ctrlKey || ev.metaKey;
  if(mod && ev.key.toLowerCase() === "a"){ ev.preventDefault(); $("selAll").click(); }
  else if(ev.key === "F2"){ ev.preventDefault(); stepMarks("bm", ev.shiftKey ? -1 : 1); }
  else if(ev.key === "F8"){ ev.preventDefault(); stepMarks("issues", ev.shiftKey ? -1 : 1); }
  else if((ev.key === "b" || ev.key === "B") && !mod && !ev.altKey && focusId !== null){ ev.preventDefault(); toggleBookmark(focusId); }
  else if(ev.key === "Escape"){ $("selNone").click(); }
  else if(mod && ev.key.toLowerCase() === "c" && !window.getSelection().toString()){
    const list = selected.size ? entries.filter(e => selected.has(e.id)) : entries.filter(e => e.id === focusId);
    if(list.length){ copy(list.map(e => e.raw).join("\n")); toast(`Copied ${fmt(list.length)} line${list.length>1?"s":""}`); }
  }
  else if(ev.key === "ArrowDown" || ev.key === "ArrowUp"){
    if(!filtered.length) return;
    ev.preventDefault();
    let i = filtered.findIndex(e => e.id === focusId);
    i = i < 0 ? (ev.key === "ArrowDown" ? 0 : filtered.length - 1) : Math.max(0, Math.min(filtered.length - 1, i + (ev.key === "ArrowDown" ? 1 : -1)));
    if(ev.shiftKey){ if(anchorIdx === null) anchorIdx = i; selectRange(anchorIdx, i); updateCounts(); }
    else anchorIdx = i;
    setFocus(filtered[i].id);
    const vp = $("vp"), y = i * ROW_H;
    if(y < vp.scrollTop) vp.scrollTop = y;
    else if(y + ROW_H > vp.scrollTop + vp.clientHeight) vp.scrollTop = y + ROW_H - vp.clientHeight;
    $("follow").checked = false;
    scheduleRender();
  }
  else if(ev.key === "Enter" && focusId !== null){ ev.preventDefault(); showInContext(focusId); }
  else if(ev.key === " " && focusId !== null){
    ev.preventDefault();
    if(selected.has(focusId)) selected.delete(focusId); else selected.add(focusId);
    scheduleRender(); updateCounts();
  }
});

$("selAll").onclick = () => { filtered.forEach(e => selected.add(e.id)); scheduleRender(); updateCounts(); };
$("selNone").onclick = () => { selected.clear(); anchorIdx = null; scheduleRender(); updateCounts(); };
$("dClose").onclick = () => { setFocus(null); scheduleRender(); };
$("dCopy").onclick = () => { const e = entries.find(x => x.id === focusId); if(e){ copy(e.raw); toast("Line copied"); } };
$("dOnlyTag").onclick = () => { const e = entries.find(x => x.id === focusId); if(e){ $("search").value = "tag:" + e.tag; onSearch(); } };
$("dIgnoreTag").onclick = () => { const e = entries.find(x => x.id === focusId); if(e) addIgnore("tag:" + e.tag); };
$("dIgnoreText").onclick = () => {
  const e = entries.find(x => x.id === focusId); if(!e) return;
  // Strip numbers/hex so the keyword survives changing values, keep a readable chunk
  const guess = e.msg.replace(/0x[0-9a-f]+|\d+(\.\d+)?/gi, " ").replace(/\s+/g, " ").trim().split(" ").slice(0, 6).join(" ");
  $("ignoreIn").value = guess; $("ignoreIn").focus(); $("ignoreIn").select();
  toast("Edit the text, then press Add");
};

// ------------------------------------------------------------------ jump to line in context
function scrollToEntry(id, frac){
  const i = filtered.findIndex(e => e.id === id);
  if(i < 0) return false;
  const vp = $("vp");
  $("follow").checked = false; stickBottom = false;
  $("spacer").style.height = (filtered.length * ROW_H) + "px"; // size the list before scrolling into it
  vp.scrollTop = Math.max(0, i * ROW_H - vp.clientHeight * frac);
  anchorIdx = i;
  setFocus(id); scheduleRender();
  return true;
}
function showInContext(id){
  if(id === null || !entries.some(e => e.id === id)) return;
  const q = $("search").value;
  if(q.trim()){ prevSearch = q; $("search").value = ""; onSearch(); }
  contextId = id;
  if(!scrollToEntry(id, 0.2)){
    toast("That line is hidden by the source, level or ignore filters. Turn them off to see it.");
    return;
  }
  updateBack();
  vp_focus();
}
function vp_focus(){ $("vp").focus({preventScroll: true}); }
function updateBack(){
  const b = $("backSearch");
  if(prevSearch === null){ b.hidden = true; return; }
  const short = prevSearch.length > 28 ? prevSearch.slice(0, 27) + "…" : prevSearch;
  b.textContent = `Back to search “${short}”`;
  b.hidden = false;
}
$("backSearch").onclick = () => {
  if(prevSearch === null) return;
  $("search").value = prevSearch; prevSearch = null; onSearch(); updateBack();
  if(contextId !== null && !scrollToEntry(contextId, 0.4)) toast("The marked line doesn't match that search.");
};
$("dContext").onclick = () => showInContext(focusId);
$("rows").addEventListener("dblclick", ev => {
  const row = ev.target.closest(".row"); if(!row || ev.target.closest(".ck")) return;
  showInContext(filtered[+row.dataset.i].id);
});

// ------------------------------------------------------------------ export
function csvCell(s){ s = String(s ?? ""); return /[",\n\r]/.test(s) ? '"' + s.replace(/"/g,'""') + '"' : s; }
function stamp(){ const d = new Date(), p = n => String(n).padStart(2,"0"); return `${d.getFullYear()}${p(d.getMonth()+1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`; }
$("export").onclick = () => {
  const scope = $("expScope").value, f = $("expFmt").value;
  const list = scope === "selected" ? entries.filter(e => selected.has(e.id)) : scope === "filtered" ? filtered
             : scope === "bookmarked" ? entries.filter(e => bookmarks.has(e.id)) : entries;
  if(!list.length && scope === "bookmarked"){ toast("No bookmarks yet. Select a line and press B."); return; }
  if(!list.length){ toast(scope === "selected" ? "No lines selected. Tick lines first, or choose “All shown lines”." : "Nothing to export yet."); return; }
  const catName = k => CATS.find(c => c[0] === k)[1];
  let body, mime;
  if(f === "csv"){
    body = "date,time,pid,tid,level,tag,source,message\r\n" + list.map(e =>
      [e.d,e.t,e.pid,e.tid,e.lvl,e.tag,catName(e.cat),e.msg].map(csvCell).join(",")).join("\r\n");
    mime = "text/csv";
  } else if(f === "json"){
    body = JSON.stringify(list.map(e => Object.assign({date:e.d, time:e.t, pid:e.pid, tid:e.tid, level:e.lvl, tag:e.tag, source:catName(e.cat), message:e.msg},
      bookmarks.get(e.id) ? {note: bookmarks.get(e.id)} : {})), null, 2);
    mime = "application/json";
  } else { body = list.map(e => e.raw).join("\n") + "\n"; mime = "text/plain"; }
  const dev = ($("device").selectedOptions[0]?.dataset.model || "headset").replace(/[^\w-]+/g, "_");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([body], {type: mime}));
  a.download = `${dev}_logs_${stamp()}.${f}`;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  toast(`Exported ${fmt(list.length)} lines`);
};

// ------------------------------------------------------------------ device / server
async function api(path, body){
  const r = await fetch(path, body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
  return r.json();
}
async function refreshDevices(){
  const sel = $("device");
  try {
    const j = await api("/api/devices");
    if(!j.ok){ sel.innerHTML = `<option value="">adb unavailable</option>`; setStatus("off", j.error); return; }
    if(!j.devices.length){ sel.innerHTML = `<option value="">No headset found</option>`; return; }
    sel.innerHTML = j.devices.map(d => {
      const name = (d.model || d.product || "Device") + " · " + d.serial + (d.state !== "device" ? ` (${d.state})` : "");
      return `<option value="${esc(d.serial)}" data-model="${esc(d.model || d.serial)}" ${d.state !== "device" ? "disabled" : ""}>${esc(name)}</option>`;
    }).join("");
    if(settings.serial && j.devices.some(d => d.serial === settings.serial)) sel.value = settings.serial;
    loadApps();
    const bad = j.devices.find(d => d.state === "unauthorized");
    if(bad) toast("Put on the headset and accept the USB debugging prompt, then Rescan.");
  } catch { setStatus("off", "Local server is not running. Start vr_log_inspector.py again."); }
}
function setStatus(kind, text){
  $("dot").className = "dot" + (kind === "on" ? " on" : kind === "off" ? " off" : "");
  $("statusText").textContent = text; $("status").title = text;
}
function applyStatus(s){
  setStatus(s.running ? "on" : "", s.message);
  $("start").disabled = !adbReady; $("stop").disabled = !s.running;
  $("start").textContent = s.running ? "Restart" : "Start";
}
$("refresh").onclick = refreshDevices;
$("device").onchange = () => { settings.serial = $("device").value; save(); loadApps(); };
$("buffers").onchange = () => { settings.buffers = $("buffers").value; save(); };
$("clearFirst").onchange = () => { settings.clearFirst = $("clearFirst").checked; save(); };
$("connect").onclick = async () => {
  const addr = $("wifiAddr").value.trim(); settings.wifi = addr; save();
  const j = await api("/api/connect", {addr});
  toast(j.output || (j.ok ? "Connected" : "Could not connect"));
  refreshDevices();
};
$("start").onclick = async () => {
  $("start").disabled = true;
  const j = await api("/api/start", {serial: $("device").value, clear: $("clearFirst").checked, buffers: $("buffers").value});
  applyStatus(j.status);
  $("follow").checked = true; stickBottom = true;
};
$("stop").onclick = async () => { applyStatus((await api("/api/stop", {})).status); };
$("clearLogs").onclick = async () => {
  entries = []; filtered = []; selected.clear(); ignoredCount = 0; setFocus(null); resetMarks();
  contextId = null; prevSearch = null; updateBack();
  recount(); updateCounts(); scheduleRender(); $("empty").style.display = "";
  try { await api("/api/clear", {}); } catch {}
};

let online = true;
async function poll(){
  try {
    const j = await api(`/api/logs?since=${cursor}&epoch=${epoch}&pver=${pver}`);
    epoch = j.epoch; cursor = j.next;
    if(j.lines.length) addLines(j.lines);
    if(j.procs){ pver = j.pver; mergeProcs(j.procs); }
    if(!online){ online = true; }
    applyStatus(j.status);
    if(j.more) return setTimeout(poll, 0);
  } catch {
    online = false;
    setStatus("off", "Local server stopped. Run vr_log_inspector.py again to keep capturing.");
    $("start").disabled = $("stop").disabled = true;
  }
  setTimeout(poll, 300);
}

// ------------------------------------------------------------------ import
async function importFiles(files){
  for(const f of files){
    const text = await f.text();
    const lines = text.split(/\r?\n/).filter(Boolean);
    stickBottom = false;
    addLines(lines);
    toast(`Loaded ${fmt(lines.length)} lines from ${f.name}`);
  }
}
$("pick").onclick = () => $("file").click();
$("file").onchange = ev => { importFiles(ev.target.files); ev.target.value = ""; };
["dragenter","dragover"].forEach(t => document.addEventListener(t, ev => { ev.preventDefault(); $("drop").classList.add("over"); }));
["dragleave","drop"].forEach(t => document.addEventListener(t, ev => { ev.preventDefault(); if(t === "drop" || !ev.relatedTarget) $("drop").classList.remove("over"); }));
document.addEventListener("drop", ev => { if(ev.dataTransfer.files.length) importFiles(ev.dataTransfer.files); });

// ------------------------------------------------------------------ misc
function copy(text){ navigator.clipboard?.writeText(text).catch(()=>{}); }
let toastT;
function toast(msg){ const t = $("toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove("show"), 2600); }

let searchT;
function onSearch(){
  compileSearch();
  hlRx = searchWords.length ? new RegExp(searchWords.map(w => esc(w).replace(/[.*+?^${}()|[\]\\]/g,"\\$&")).join("|"), "gi") : null;
  rebuild(true);
}
$("search").addEventListener("input", () => { prevSearch = null; updateBack(); clearTimeout(searchT); searchT = setTimeout(onSearch, 150); });

$("ignoreAdd").onclick = () => { addIgnore($("ignoreIn").value); $("ignoreIn").value = ""; };
$("ignoreIn").addEventListener("keydown", ev => { if(ev.key === "Enter") $("ignoreAdd").click(); });
$("chips").addEventListener("click", ev => {
  const del = ev.target.closest("[data-del]"), tog = ev.target.closest("span[data-i]");
  if(del) settings.ignore.splice(+del.dataset.del, 1);
  else if(tog){ const k = settings.ignore[+tog.dataset.i]; k.on = !k.on; }
  else return;
  save(); compileIgnore(); renderChips(); rebuild(true);
});
$("ignoreOn").onchange = () => { settings.ignoreOn = $("ignoreOn").checked; save(); rebuild(true); };

function onRules(){
  settings.unityTags = $("unityTags").value; settings.xrTags = $("xrTags").value; save();
  compileRules(); for(const e of entries) e.cat = categorize(e.tag);
  recount(); rebuild(true);
}
let rulesT;
$("unityTags").addEventListener("input", () => { clearTimeout(rulesT); rulesT = setTimeout(onRules, 300); });
$("xrTags").addEventListener("input", () => { clearTimeout(rulesT); rulesT = setTimeout(onRules, 300); });
$("rulesReset").onclick = () => { $("unityTags").value = DEFAULT_UNITY; $("xrTags").value = DEFAULT_XR; onRules(); };
$("expScope").onchange = () => { settings.expScope = $("expScope").value; save(); };
$("expFmt").onchange = () => { settings.expFmt = $("expFmt").value; save(); };

$("appAdd").onclick = () => { if(addApp($("appIn").value)){ $("appIn").value = ""; rebuild(true); } };
$("appIn").addEventListener("keydown", ev => { if(ev.key === "Enter") $("appAdd").click(); });
$("appOpen").onclick = async () => {
  const j = await loadApps();
  if(j && j.foreground){ if(addApp(j.foreground)) rebuild(true); }
  else toast("Couldn't detect the open app. Open it in the headset and try again.");
};
$("appChips").addEventListener("click", ev => {
  const del = ev.target.closest("[data-del]"), tog = ev.target.closest("span[data-i]");
  if(del) settings.apps.splice(+del.dataset.del, 1);
  else if(tog){ const a = settings.apps[+tog.dataset.i]; a.on = !a.on; }
  else return;
  save(); compileApps(); renderApps(); rebuild(true);
});
$("appsOn").onchange = () => { settings.appsOn = $("appsOn").checked; save(); rebuild(true); drawerDirty = true; updateMarkCounts(); renderDrawer(); };
$("appFollow").onchange = () => { settings.appFollow = $("appFollow").checked; save(); };
$("appMention").onchange = () => { settings.appMention = $("appMention").checked; save(); rebuild(true); };

// ------------------------------------------------------------------ issues & bookmarks
const ISSUE = {crash:"Crash", native:"Native crash", anr:"ANR", exception:"Exception", oom:"Out of memory",
               died:"App died", lmk:"Low-memory kill", perf:"FPS drop"};
let issues = [], issueByKey = new Map(), issueSeq = 0, lowFps = 0;
const bookmarks = new Map();          // entry id -> note
const crashByPid = new Map();         // pid -> crash issue, to pick up the exception line that follows
let drawerTab = "issues", drawerDirty = true;
const RE_EXC = /\b([A-Z]\w*(?:\.\w+)*(?:Exception|Error))\b/;
const RE_DIED = /^Process (\S+) \(pid (\d+)\) has died/;
const RE_LMK = /Kill(?:ing)? '?([\w.:]+)/;
const RE_FPS = /^FPS=(\d+)\/(\d+)/;
const normKey = s => s.replace(/0x[0-9a-f]+|\d+/gi, "#").slice(0, 140);
const procOf = pid => pidName.get(pid) || "";
function entryById(id){
  if(!entries.length) return null;
  const e = entries[id - entries[0].id];
  return e && e.id === id ? e : entries.find(x => x.id === id) || null;
}
// group = true: identical problems (same text, numbers ignored) are counted on one issue
function addIssue(kind, e, title, sub, group){
  const key = group ? kind + "|" + normKey(title) : kind + "|" + e.id;
  let it = issueByKey.get(key);
  if(it){ it.count++; drawerDirty = true; return it; }
  if(issues.length >= 2000) return null;
  it = {id: ++issueSeq, kind, entryId: e.id, title: title.slice(0, 300), sub: (sub || "").slice(0, 300), time: e.t, pid: e.pid, count: 1, key};
  issues.push(it); issueByKey.set(key, it); drawerDirty = true;
  return it;
}
function detectIssue(e){
  const m0 = e.msg;
  if(e.tag === "AndroidRuntime"){
    if(m0.startsWith("FATAL EXCEPTION")){
      const it = addIssue("crash", e, "Java crash in {proc}", m0, false);
      if(it) crashByPid.set(e.pid, it);
    } else {
      const c = crashByPid.get(e.pid);
      if(c && !c.detailed && !m0.startsWith("Process:") && RE_EXC.test(m0)){ c.detailed = true; c.sub = m0.slice(0, 300); drawerDirty = true; }
    }
    return;
  }
  if(m0.includes("Fatal signal")){ addIssue("native", e, "Native crash in {proc}", m0, false); return; }
  if((e.tag === "ActivityManager" || e.tag === "ActivityTaskManager") && m0.startsWith("ANR in")){ addIssue("anr", e, m0, "", false); return; }
  if(e.tag === "ActivityManager"){
    const m = RE_DIED.exec(m0);
    if(m){ addIssue("died", e, "Process died: " + m[1], m0, false); return; }
  }
  if(/lowmemorykiller|lmkd/i.test(e.tag)){
    const m = RE_LMK.exec(m0);
    if(m){ addIssue("lmk", e, "Killed for low memory: " + m[1], m0, false); return; }
  }
  if(m0.includes("OutOfMemoryError")){ addIssue("oom", e, "OutOfMemoryError in {proc}", m0, true); return; }
  if((e.tag === "Unity" || e.tag === "UnityMain") && (e.lvl === "E" || e.lvl === "F")){
    const m = RE_EXC.exec(m0);
    if(m && m[1].endsWith("Exception")){ addIssue("exception", e, m0, "", true); return; }
  }
  if(e.tag === "VrApi"){
    const m = RE_FPS.exec(m0);
    if(m){
      const fps = +m[1], tgt = +m[2];
      if(tgt && fps < 0.85 * tgt){ if(++lowFps === 3) addIssue("perf", e, `FPS ${fps}/${tgt} for 3+ seconds`, "", false); }
      else lowFps = 0;
    }
  }
}
function issueVisible(it){
  if(!appActive() || !settings.issuesAppOnly) return true;
  const e = entryById(it.entryId);
  return e ? appMatch(e) : true;
}
const visibleIssues = () => issues.filter(issueVisible);
function pruneMarks(){
  // drop issues and bookmarks whose lines were discarded to stay under the line limit
  const first = entries.length ? entries[0].id : Infinity;
  issues = issues.filter(it => it.entryId >= first);
  issueByKey = new Map(issues.map(it => [it.key, it]));
  for(const id of [...bookmarks.keys()]) if(id < first) bookmarks.delete(id);
  crashByPid.clear(); drawerDirty = true;
}
function resetMarks(){
  issues = []; issueByKey = new Map(); bookmarks.clear(); crashByPid.clear(); lowFps = 0; drawerDirty = true;
  updateMarkCounts(); renderDrawer();
}
function updateMarkCounts(){
  const n = visibleIssues().length;
  $("issuesCnt").textContent = n;
  $("issuesCnt").classList.toggle("hot", n > 0);
  $("bmCnt").textContent = bookmarks.size;
}
function goToEntry(id){
  if(!entryById(id)){ toast("That line is no longer in memory."); return; }
  if(filtered.some(e => e.id === id)) scrollToEntry(id, 0.3);
  else showInContext(id);
}
function stepMarks(kind, dir){
  const ids = kind === "bm" ? [...bookmarks.keys()].sort((a, b) => a - b) : visibleIssues().map(it => it.entryId);
  if(!ids.length){ toast(kind === "bm" ? "No bookmarks yet. Select a line and press B." : "No issues found so far."); return; }
  const ref = focusId === null ? (dir > 0 ? -Infinity : Infinity) : focusId;
  let target = dir > 0 ? ids.find(id => id > ref) : [...ids].reverse().find(id => id < ref);
  if(target === undefined){ target = dir > 0 ? ids[0] : ids[ids.length - 1]; toast(dir > 0 ? "Back to the first one" : "Back to the last one"); }
  goToEntry(target);
}
function toggleBookmark(id){
  if(id === null) return;
  if(bookmarks.has(id)){ bookmarks.delete(id); toast("Bookmark removed"); }
  else { bookmarks.set(id, ""); toast("Bookmarked. Add a note from the Bookmarks list."); }
  drawerDirty = true; updateMarkCounts(); renderDrawer(); scheduleRender(); syncBmButton();
}
function syncBmButton(){ $("dBm").textContent = focusId !== null && bookmarks.has(focusId) ? "Remove bookmark" : "Bookmark"; }
function openDrawer(tab){ drawerTab = tab; $("drawer").hidden = false; drawerDirty = true; renderDrawer(); }
function toggleDrawer(tab){ if(!$("drawer").hidden && drawerTab === tab) $("drawer").hidden = true; else openDrawer(tab); }
function renderDrawer(){
  if($("drawer").hidden || !drawerDirty) return;
  drawerDirty = false;
  $("tabIssues").setAttribute("aria-selected", drawerTab === "issues");
  $("tabBm").setAttribute("aria-selected", drawerTab === "bm");
  const list = $("dwList"), note = $("dwNote");
  if(drawerTab === "issues"){
    const vis = visibleIssues(), byKind = {};
    vis.forEach(it => byKind[it.kind] = (byKind[it.kind] || 0) + it.count);
    note.innerHTML = `<span>${vis.length ? Object.keys(byKind).map(k => `<span class="kb k-${k}">${byKind[k]} ${ISSUE[k]}</span>`).join(" ") : "No issues found so far."}</span>` +
      (appActive() ? `<label class="chk" style="margin-left:auto"><input type="checkbox" id="dwAppOnly" ${settings.issuesAppOnly ? "checked" : ""}> Only selected apps</label>` : "");
    list.innerHTML = vis.length ? vis.map(it => {
      const proc = procOf(it.pid);
      return `<div class="it${it.entryId === focusId ? " cur" : ""}" tabindex="0" data-id="${it.entryId}">` +
        `<div class="top"><span class="kb k-${it.kind}">${ISSUE[it.kind]}</span><span class="when">${esc(it.time.slice(0, 8))}</span>${it.count > 1 ? `<span class="cnt">×${it.count}</span>` : ""}</div>` +
        `<div class="ttl">${esc(it.title.replace("{proc}", proc || (it.pid ? "pid " + it.pid : "unknown process")))}</div>` +
        (it.sub ? `<div class="sub">${esc(it.sub)}</div>` : (proc && !it.title.includes("{proc}") ? `<div class="sub">${esc(proc)}</div>` : "")) + `</div>`;
    }).join("") : `<div class="empty2">Crashes, native crashes, ANRs, Unity exceptions, out-of-memory errors, apps that die or are killed for low memory, and FPS drops on Quest show up here as they happen.</div>`;
  } else {
    const ids = [...bookmarks.keys()].sort((a, b) => a - b);
    note.innerHTML = `<span>${ids.length} bookmark${ids.length === 1 ? "" : "s"}</span>` +
      (ids.length ? `<button id="dwBmExport" style="margin-left:auto">Export</button><button id="dwBmClear">Clear all</button>` : "");
    list.innerHTML = ids.length ? ids.map(id => {
      const e = entryById(id); if(!e) return "";
      return `<div class="it${id === focusId ? " cur" : ""}" tabindex="0" data-id="${id}">` +
        `<div class="top"><span class="when">${esc(e.t.slice(0, 12))}</span><span class="kb" style="color:var(--l-${e.lvl.toLowerCase()})">${e.lvl}</span><span>${esc(e.tag)}</span><button class="x" data-del="${id}" title="Remove bookmark">✕</button></div>` +
        `<div class="ttl">${esc(e.msg.slice(0, 220))}</div>` +
        `<input type="text" data-note="${id}" placeholder="Add a note…" value="${esc(bookmarks.get(id) || "")}"></div>`;
    }).join("") : `<div class="empty2">Select a line and press <b>B</b> (or the Bookmark button in the line details) to keep it here. F2 and Shift+F2 jump between bookmarks.</div>`;
  }
}
$("issuesBtn").onclick = () => toggleDrawer("issues");
$("bmBtn").onclick = () => toggleDrawer("bm");
$("tabIssues").onclick = () => openDrawer("issues");
$("tabBm").onclick = () => openDrawer("bm");
$("dwClose").onclick = () => { $("drawer").hidden = true; };
$("dwNext").onclick = () => stepMarks(drawerTab, 1);
$("dwPrev").onclick = () => stepMarks(drawerTab, -1);
$("dBm").onclick = () => toggleBookmark(focusId);
$("dwList").addEventListener("click", ev => {
  if(ev.target.closest("input")) return;
  const del = ev.target.closest("[data-del]");
  if(del){ bookmarks.delete(+del.dataset.del); drawerDirty = true; updateMarkCounts(); renderDrawer(); scheduleRender(); syncBmButton(); return; }
  const it = ev.target.closest(".it"); if(it) goToEntry(+it.dataset.id);
});
$("dwList").addEventListener("keydown", ev => {
  const it = ev.target.closest(".it");
  if(ev.key === "Enter" && it && !ev.target.closest("input")){ ev.preventDefault(); goToEntry(+it.dataset.id); }
});
$("dwList").addEventListener("input", ev => { const n = ev.target.dataset.note; if(n !== undefined) bookmarks.set(+n, ev.target.value); });
$("dwNote").addEventListener("change", ev => {
  if(ev.target.id === "dwAppOnly"){ settings.issuesAppOnly = ev.target.checked; save(); drawerDirty = true; updateMarkCounts(); renderDrawer(); }
});
$("dwNote").addEventListener("click", ev => {
  if(ev.target.id === "dwBmExport"){ $("expScope").value = "bookmarked"; $("export").click(); }
  if(ev.target.id === "dwBmClear"){ bookmarks.clear(); drawerDirty = true; updateMarkCounts(); renderDrawer(); scheduleRender(); syncBmButton(); toast("Bookmarks cleared"); }
});

// ------------------------------------------------------------------ appearance
const THEMES = [
  ["graphite", "Graphite", ["#1D2024", "#16181B", "#8FD3B0", "#79C0FF", "#FF7B72"]],
  ["graphite-light", "Graphite light", ["#FFFFFF", "#F7F8FA", "#1F7A57", "#0757B8", "#C4161C"]],
  ["system", "Match system", ["#1D2024", "#F7F8FA", "#8FD3B0", "#1F7A57", "#C4161C"]],
  ["midnight", "Midnight", ["#1b2033", "#141827", "#8e9dff", "#8fbaff", "#ff8585"]],
  ["daylight", "Daylight", ["#ffffff", "#eef0f7", "#4655d6", "#1d52ad", "#ad1f1f"]],
  ["contrast", "High contrast", ["#000000", "#000000", "#FFD400", "#8FD0FF", "#FF7A7A"]],
  ["ludus", "Ludus", ["#0A2428", "#0F3035", "#35E398", "#4FC3F7", "#FF8A8A"]]
];
const DENSITY = {compact: 20, normal: 22, comfy: 27};
const FONT_STEPS = [0.85, 1, 1.15, 1.3, 1.5, 1.75, 2];
const hueCache = new Map();
function tagHue(tag){
  let h = hueCache.get(tag);
  if(h === undefined){
    let x = 2166136261;
    for(let i = 0; i < tag.length; i++){ x ^= tag.charCodeAt(i); x = Math.imul(x, 16777619); }
    h = (x >>> 0) % 360;
    hueCache.set(tag, h);
  }
  return h;
}
const sysLight = matchMedia("(prefers-color-scheme: light)");
function applyAppearance(){
  const t = settings.theme === "system" ? (sysLight.matches ? "graphite-light" : "graphite") : settings.theme;
  document.documentElement.dataset.theme = t;
  const vp = $("vp");
  vp.classList.toggle("colortags", settings.colorTags);
  vp.classList.toggle("tint", settings.tintRows);
  vp.classList.toggle("zebra", settings.zebra);
  const firstVisible = Math.floor(vp.scrollTop / ROW_H);
  const fs = settings.fontScale || 1;
  document.documentElement.style.setProperty("--fs", fs);
  ROW_H = Math.round((DENSITY[settings.density] || 22) * fs);
  $("fsVal").textContent = Math.round(fs * 100) + "%";
  fitColumns();
  $("fsDown").disabled = $("fsDownQ").disabled = fs <= FONT_STEPS[0];
  $("fsUp").disabled = $("fsUpQ").disabled = fs >= FONT_STEPS[FONT_STEPS.length - 1];
  document.documentElement.style.setProperty("--row-h", ROW_H + "px");
  $("spacer").style.height = (filtered.length * ROW_H) + "px";
  vp.scrollTop = firstVisible * ROW_H;   // keep the same lines in view when the row height changes
  $("themes").querySelectorAll(".th").forEach(b => b.setAttribute("aria-checked", b.dataset.t === settings.theme));
  $("density").querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", b.dataset.d === settings.density));
  $("optTags").checked = settings.colorTags; $("optTint").checked = settings.tintRows; $("optZebra").checked = settings.zebra;
  scheduleRender();
}
$("themes").innerHTML = THEMES.map(([k, name, c]) =>
  `<button class="th" role="radio" data-t="${k}" aria-checked="false"><span class="pv" style="background:${c[1]}"><i style="background:${c[0]}"></i>` +
  `<b><span style="background:${c[2]};width:70%"></span><span style="background:${c[3]};width:45%"></span><span style="background:${c[4]};width:58%"></span></b></span>` +
  `<span class="nm">${name}</span></button>`).join("");
$("themes").addEventListener("click", ev => { const b = ev.target.closest(".th"); if(!b) return; settings.theme = b.dataset.t; save(); applyAppearance(); });
$("density").addEventListener("click", ev => { const b = ev.target.closest("button"); if(!b) return; settings.density = b.dataset.d; save(); applyAppearance(); });
[["optTags","colorTags"],["optTint","tintRows"],["optZebra","zebra"]].forEach(([id, key]) =>
  $(id).addEventListener("change", () => { settings[key] = $(id).checked; save(); applyAppearance(); }));
sysLight.addEventListener("change", () => { if(settings.theme === "system") applyAppearance(); });
function stepFont(dir){
  const cur = settings.fontScale || 1;
  const next = dir > 0 ? FONT_STEPS.find(v => v > cur + 1e-6) : [...FONT_STEPS].reverse().find(v => v < cur - 1e-6);
  if(next === undefined) return;
  settings.fontScale = next; save(); applyAppearance();
  toast("Text size " + Math.round(next * 100) + "%");
}
$("fsUp").onclick = $("fsUpQ").onclick = () => stepFont(1);
$("fsDown").onclick = $("fsDownQ").onclick = () => stepFont(-1);
function fitColumns(){
  const fs = settings.fontScale || 1, w = $("vp").clientWidth, sec = document.querySelector("section.logs");
  if(!w) return;
  sec.classList.toggle("nopid", w < 900 * fs);
  sec.classList.toggle("notime", w < 600 * fs);
}
new ResizeObserver(fitColumns).observe($("vp"));
function applySide(){
  $("main").classList.toggle("noside", settings.sideHidden);
  $("sideBtn").textContent = settings.sideHidden ? "Show filters" : "Hide filters";
  $("sideBtn").setAttribute("aria-expanded", !settings.sideHidden);
}
$("sideBtn").onclick = () => { settings.sideHidden = !settings.sideHidden; save(); applySide(); };
$("fsReset").onclick = () => { settings.fontScale = 1; save(); applyAppearance(); toast("Text size 100%"); };
function setAppear(open){
  $("appear").hidden = !open; $("appearBtn").setAttribute("aria-expanded", open);
  if(open) $("themes").querySelector('[aria-checked="true"]')?.focus();
}
$("appearBtn").onclick = ev => { ev.stopPropagation(); setAppear($("appear").hidden); };
document.addEventListener("click", ev => { if(!$("appear").hidden && !ev.target.closest("#appear")) setAppear(false); });
document.addEventListener("keydown", ev => { if(ev.key === "Escape" && !$("appear").hidden){ setAppear(false); $("appearBtn").focus(); } }, true);

// ------------------------------------------------------------------ adb setup
let adbPollT = null, adbReady = true;
const mb = n => (n / 1048576).toFixed(1);
function renderAdb(st){
  const bar = $("adbBar"), txt = $("adbTxt"), btn = $("adbGet");
  adbReady = st.state === "ok";
  $("start").disabled = !adbReady;
  bar.classList.toggle("err", st.state === "error");
  $("adbProg").hidden = st.state !== "downloading";
  if(st.state === "ok"){
    if(!bar.hidden && bar.dataset.was !== "ok"){
      txt.innerHTML = "<b>adb is ready.</b> Connect the headset and pick it in the list.";
      btn.hidden = true; bar.dataset.was = "ok";
      setTimeout(() => { bar.hidden = true; }, 5000);
      refreshDevices();
    }
    return;
  }
  bar.hidden = false; bar.dataset.was = st.state; btn.hidden = st.state === "downloading";
  if(st.state === "downloading"){
    const pct = st.total ? Math.round(100 * st.received / st.total) : 0;
    $("adbFill").style.width = (st.total ? pct : 15) + "%";
    txt.innerHTML = `<b>${esc(st.message || "Downloading adb…")}</b><small>${st.total ? mb(st.received) + " of " + mb(st.total) + " MB" : mb(st.received) + " MB"}</small>`;
  } else if(st.state === "error"){
    btn.textContent = "Try again";
    txt.innerHTML = `<b>${esc(st.message)}</b><small>You can also download platform-tools yourself from <a href="${esc(st.page)}" target="_blank" rel="noopener">Google's page</a>, unzip it next to this tool and restart it.</small>`;
  } else {
    btn.textContent = "Download adb";
    txt.innerHTML = `<b>adb isn't installed.</b> VR Log Inspector needs Google's Android platform-tools to talk to the headset.` +
      `<small>It's saved in a <code>platform-tools</code> folder next to this tool: no admin rights or PATH changes. ` +
      `Downloading means you accept the <a href="${esc(st.page)}" target="_blank" rel="noopener">platform-tools terms</a>. ` +
      `Already have adb somewhere? Start the tool with <code>--adb "path/to/adb"</code>.</small>`;
  }
}
async function checkAdb(){
  clearTimeout(adbPollT);
  try {
    const st = await api("/api/adb");
    renderAdb(st);
    if(st.state === "downloading") adbPollT = setTimeout(checkAdb, 400);
  } catch {}
}
$("adbGet").onclick = async () => { renderAdb(await api("/api/adb/download", {})); checkAdb(); };

// ------------------------------------------------------------------ init
$("appsOn").checked = settings.appsOn; $("appFollow").checked = settings.appFollow; $("appMention").checked = settings.appMention;
compileApps(); renderApps(); applySide(); applyAppearance();
$("unityTags").value = settings.unityTags; $("xrTags").value = settings.xrTags;
$("ignoreOn").checked = settings.ignoreOn; $("buffers").value = settings.buffers;
$("clearFirst").checked = settings.clearFirst; $("wifiAddr").value = settings.wifi || "";
$("expScope").value = settings.expScope; $("expFmt").value = settings.expFmt;
compileRules(); compileIgnore(); compileSearch();
buildToggles(); renderChips(); recount(); updateCounts();
onToggle($("cats"), catOn, "cats"); onToggle($("levels"), levelOn, "levels");
checkAdb(); refreshDevices(); poll();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
