#!/usr/bin/env python3
"""
VR Session Recorder
===================
One-button bug session capture for Android VR headsets (Meta Quest 3, Pico 4 Ultra Enterprise...).

While recording it captures, at the same time:
  * the full logcat (main, system and crash buffers)
  * a video of the headset view (scrcpy on the PC, or the headset's own screenrecord)
  * performance metrics (FPS, stale frames, CPU/GPU levels and load, temperature on Quest;
    battery, thermal status and app memory on any headset)
  * markers: your notes, screenshots taken in the headset, crashes, ANRs, Unity exceptions,
    FPS drops and app restarts are timestamped automatically

When you stop, everything is packaged in a folder and a .zip with report.html: video with
clickable markers, performance charts, error summary and the log lines around every marker.

Requirements: Python 3.8+ (standard library only), adb on PATH.
Optional: scrcpy on PATH for PC-side video recording (recommended).

Usage:
  python vr_session_recorder.py
  python vr_session_recorder.py --out D:/QA/sessions --port 8766
"""

import argparse
import csv
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
import zipfile
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

TOOL_VERSION = "0.1"
IS_WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WIN else 0
PIPE, STDOUT, DEVNULL = subprocess.PIPE, subprocess.STDOUT, subprocess.DEVNULL

ADB = "adb"
SCRCPY = None
SCRCPY_VER = None
OUT_DIR = ""
CURRENT = None
CURRENT_LOCK = threading.Lock()

LINE_RE = re.compile(r"^((?:\d{4}-)?\d\d-\d\d)\s+(\d\d:\d\d:\d\d\.\d+)\s+(\d+)\s+(\d+)\s+([VDIWEFAS])\s+(.*?)\s*: ?(.*)$")
EXC_RE = re.compile(r"\b[A-Z]\w*(?:Exception|Error)\b")
PICO_FPS_RE = re.compile(r"\bfps\s*[=:]\s*(\d+(?:\.\d+)?)", re.I)
NORM_RE = re.compile(r"0x[0-9a-fA-F]+|\d+")
PKG_RE = re.compile(r"^[A-Za-z][\w]*(\.[\w]+)+$")
STAMP = "/data/local/tmp/vrsr_stamp"
SHOT_DIRS = "/sdcard/Oculus /sdcard/Pictures /sdcard/DCIM /sdcard/Pico /sdcard/Screenshots"
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")
VR_COLS = ["t", "fps", "target", "stale", "cpu_lvl", "gpu_lvl", "cpu_util", "gpu_util", "temp", "free_mb"]
DEV_COLS = ["t", "battery", "bat_temp", "thermal", "pid", "app_mem"]


# --------------------------------------------------------------------------- helpers
def run(cmd, timeout=15):
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=timeout, creationflags=NO_WINDOW)
        return res.returncode == 0, (res.stdout or "") + (res.stderr or "")
    except FileNotFoundError:
        return False, f"'{cmd[0]}' not found"
    except subprocess.TimeoutExpired:
        return False, "timed out"


def adb(serial, args, timeout=15):
    return run([ADB] + (["-s", serial] if serial else []) + args, timeout)


def shell(serial, command, timeout=15):
    return adb(serial, ["shell", command], timeout)


def find_adb(explicit):
    if explicit:
        return explicit
    found = shutil.which("adb")
    if found:
        return found
    exe = "adb.exe" if IS_WIN else "adb"
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(env)
        if root and os.path.isfile(os.path.join(root, "platform-tools", exe)):
            return os.path.join(root, "platform-tools", exe)
    return "adb"


def detect_scrcpy(explicit):
    path = explicit or shutil.which("scrcpy")
    if not path:
        return None, None
    ok, out = run([path, "--version"], 10)
    m = re.search(r"scrcpy\s+v?(\d+)\.(\d+)", out)
    return path, ((int(m.group(1)), int(m.group(2))) if m else None)


def slug(s, n=40):
    return (re.sub(r"[^\w.-]+", "_", s or "").strip("_.") or "x")[:n]


def clock(sec):
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def open_path(path):
    if IS_WIN:
        os.startfile(path)  # noqa
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def list_devices():
    ok, out = adb(None, ["devices", "-l"])
    if not ok:
        return {"ok": False, "error": out.strip(), "devices": []}
    devices = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith(("List of devices", "*")):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        info = dict(p.split(":", 1) for p in parts[2:] if ":" in p)
        devices.append({"serial": parts[0], "state": parts[1],
                        "model": info.get("model", "").replace("_", " ")})
    return {"ok": True, "devices": devices}


def device_info(serial):
    ok, out = shell(serial, "getprop", 15)
    props = dict(re.findall(r"^\[([^\]]+)\]: \[([^\]]*)\]", out, re.M))
    return {"serial": serial,
            "model": props.get("ro.product.model", ""),
            "manufacturer": props.get("ro.product.manufacturer", ""),
            "build": props.get("ro.build.display.id", ""),
            "android": props.get("ro.build.version.release", "")}


def app_info(serial, pkg):
    ok, out = shell(serial, f"dumpsys package {pkg}", 20)
    vn = re.search(r"versionName=(\S+)", out)
    vc = re.search(r"versionCode=(\d+)", out)
    return {"pkg": pkg, "version_name": vn.group(1) if vn else "",
            "version_code": vc.group(1) if vc else "", "installed": bool(vn or vc)}


def list_apps(serial):
    ok, out = shell(serial, "pm list packages -3", 20)
    apps = sorted({l.split(":", 1)[1].strip() for l in out.splitlines() if l.startswith("package:")})
    ok, out = shell(serial, 'dumpsys activity activities | grep -E "ResumedActivity"', 20)
    m = re.search(r"\s([A-Za-z][\w.]+)/[\w.$]+", out)
    return {"apps": apps, "foreground": m.group(1) if m else ""}


# --------------------------------------------------------------------------- session
class Session:
    def __init__(self, serial, opts):
        self.serial = serial
        self.opts = opts
        self.pkg = (opts.get("pkg") or "").strip()
        self.id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.started_iso = datetime.now().isoformat(timespec="seconds")
        self.lock = threading.RLock()
        self.stop_ev = threading.Event()
        self.state = "starting"
        self.status = "Preparing the headset…"
        self.lines = []
        self.vr = []
        self.dev = []
        self.markers = []
        self.counts = {"W": 0, "E": 0, "F": 0}
        self.video = {"mode": opts.get("video", "none"), "parts": [], "error": ""}
        self.bug = {"title": opts.get("title", ""), "tester": opts.get("tester", ""),
                    "severity": "", "steps": "", "expected": "", "actual": ""}
        self.device, self.app = {}, {}
        self.t0 = self.t_end = None
        self.threads = []
        self.logproc = self.vproc = None
        self.logf = None
        self.app_pid = None
        self.recent = {}
        self.low_fps = 0
        self.folder = self.name = None

    # -- basics
    def elapsed(self):
        if self.t0 is None:
            return 0.0
        return (self.t_end or time.time()) - self.t0

    def _thread(self, fn):
        t = threading.Thread(target=fn, daemon=True)
        t.start()
        self.threads.append(t)

    def add_marker(self, kind, text, line=None, file=None, auto=False):
        with self.lock:
            m = {"id": len(self.markers) + 1, "t": round(self.elapsed(), 2), "kind": kind,
                 "text": (text or "")[:400], "line": len(self.lines) if line is None else line,
                 "file": file, "auto": auto}
            self.markers.append(m)
            return m

    def _auto_marker(self, kind, text, idx):
        key = (kind, NORM_RE.sub("#", text)[:100])
        now = time.time()
        if now - self.recent.get(key, 0) < 10:
            return
        self.recent[key] = now
        if sum(1 for m in self.markers if m["auto"]) >= 300:
            return
        self.add_marker(kind, text, idx, auto=True)

    # -- start
    def start(self):
        try:
            self._start()
        except Exception as e:  # report any failure in the UI instead of dying silently
            self.state = "error"
            self.status = f"Could not start: {e}"
            self.stop_ev.set()
            for p in (self.logproc, self.vproc):
                if p and p.poll() is None:
                    p.kill()

    def _start(self):
        s = self.serial
        self.device = device_info(s)
        if not self.device.get("model"):
            raise RuntimeError("the headset did not answer. Check the cable, accept USB debugging and press Rescan.")
        if self.pkg:
            self.app = app_info(s, self.pkg)
        model = self.device.get("model") or "headset"
        self.name = f"{self.id}_{slug(model, 24)}" + (f"_{slug(self.pkg.split('.')[-1], 24)}" if self.pkg else "")
        self.folder = os.path.join(OUT_DIR, self.name)
        os.makedirs(os.path.join(self.folder, "screenshots"), exist_ok=True)

        if self.opts.get("clear_log"):
            adb(s, ["logcat", "-c"])
        restart = bool(self.opts.get("restart_app") and self.pkg)
        if restart:
            shell(s, f"am force-stop {self.pkg}")
        shell(s, f"touch {STAMP}")

        self.t0 = time.time()
        self.logf = open(os.path.join(self.folder, "logcat.log"), "w", encoding="utf-8", newline="\n")
        self.logproc = subprocess.Popen([ADB, "-s", s, "logcat", "-v", "threadtime", "-b", "main,system,crash"],
                                        stdout=PIPE, stderr=STDOUT, stdin=DEVNULL, creationflags=NO_WINDOW)
        self._thread(self._logcat)

        mode = self.video["mode"]
        if mode == "scrcpy":
            if not SCRCPY:
                self.video["error"] = "scrcpy is not installed, so no video was recorded."
            else:
                self._start_scrcpy()
        elif mode == "screenrecord":
            self._thread(self._screenrecord)

        if restart:
            shell(s, f"monkey -p {self.pkg} -c android.intent.category.LAUNCHER 1")
            self.add_marker("app", f"App launched by the recorder ({self.pkg})", auto=True)

        self._thread(self._poller)
        self._thread(self._shots)
        self.state = "recording"
        self.status = "Recording"

    # -- logcat
    def _logcat(self):
        proc = self.logproc
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                continue
            idx = len(self.lines)
            self.lines.append(line)
            try:
                self.logf.write(line + "\n")
            except ValueError:
                pass
            m = LINE_RE.match(line)
            if m:
                try:
                    self._analyze(idx, m.group(5), m.group(6).strip(), m.group(7))
                except Exception:
                    pass
        if not self.stop_ev.is_set():
            self.status = "The headset log stopped (cable, sleep or Wi-Fi?). Press Stop to package what was captured."
            self.add_marker("system", "Log capture stopped: headset disconnected?", auto=True)

    def _analyze(self, idx, lvl, tag, msg):
        if lvl == "A":
            lvl = "F"
        if lvl in self.counts:
            self.counts[lvl] += 1
        if tag == "VrApi" and msg.startswith("FPS="):
            self._vrapi(msg)
            return
        tl = tag.lower()
        if "pxr" in tl or "pico" in tl:
            m = PICO_FPS_RE.search(msg)
            if m:
                self._fps_sample(float(m.group(1)), None, {})
        kind = text = None
        if tag == "AndroidRuntime" and msg.startswith("FATAL EXCEPTION"):
            kind, text = "crash", "Java crash: " + msg
        elif "Fatal signal" in msg:
            kind, text = "crash", "Native crash: " + msg
        elif tag == "ActivityManager" and msg.startswith("ANR in"):
            kind, text = "anr", msg
        elif tag in ("Unity", "UnityMain") and lvl in ("E", "F") and EXC_RE.search(msg):
            kind, text = "exception", msg
        elif lvl == "F":
            kind, text = "crash", f"{tag}: {msg}"
        if kind:
            self._auto_marker(kind, text, idx)

    def _vrapi(self, msg):
        m = re.search(r"FPS=(\d+)/(\d+)", msg)
        if not m:
            return

        def f(p):
            r = re.search(p, msg)
            return float(r.group(1)) if r else None

        lv = re.search(r"CPU\d*/GPU=(\d+)/(\d+)", msg)
        self._fps_sample(int(m.group(1)), int(m.group(2)), {
            "stale": f(r"Stale=(\d+)"),
            "cpu_lvl": int(lv.group(1)) if lv else None, "gpu_lvl": int(lv.group(2)) if lv else None,
            "cpu_util": f(r"CPU%=([\d.]+)"), "gpu_util": f(r"GPU%=([\d.]+)"),
            "temp": f(r"Temp=([\d.]+)C"), "free_mb": f(r"Free=(\d+)MB")})

    def _fps_sample(self, fps, target, extra):
        s = {"t": round(self.elapsed(), 1), "fps": fps, "target": target}
        s.update(extra)
        self.vr.append(s)
        if target and fps < 0.85 * target:
            self.low_fps += 1
        else:
            self.low_fps = 0
        if self.low_fps == 3:
            self._auto_marker("perf", f"FPS drop: {fps:g}/{target} for 3+ seconds", len(self.lines))

    # -- periodic device metrics
    def _poller(self):
        interval = max(2, int(self.opts.get("interval") or 5))
        first = True
        while not self.stop_ev.is_set():
            s = {"t": round(self.elapsed(), 1)}
            _, out = shell(self.serial, "dumpsys battery", 10)
            m = re.search(r"^\s*level:\s*(\d+)", out, re.M)
            s["battery"] = int(m.group(1)) if m else None
            m = re.search(r"^\s*temperature:\s*(\d+)", out, re.M)
            s["bat_temp"] = int(m.group(1)) / 10 if m else None
            _, out = shell(self.serial, 'dumpsys thermalservice | grep -m1 "Thermal Status"', 10)
            m = re.search(r"Thermal Status:\s*(\d+)", out)
            s["thermal"] = int(m.group(1)) if m else None
            if self.pkg:
                _, out = shell(self.serial, f"pidof {self.pkg}", 10)
                tok = out.strip().split()
                pid = tok[0] if tok and tok[0].isdigit() else None
                if not first and not self.stop_ev.is_set():
                    if self.app_pid and not pid:
                        self.add_marker("app", "App process is gone (closed or crashed)", auto=True)
                    elif self.app_pid and pid and pid != self.app_pid:
                        self.add_marker("app", f"App restarted (PID {self.app_pid} → {pid})", auto=True)
                    elif not self.app_pid and pid:
                        self.add_marker("app", f"App started (PID {pid})", auto=True)
                self.app_pid = pid
                s["pid"] = pid
                if pid:
                    _, out = shell(self.serial, f"dumpsys meminfo {self.pkg}", 20)
                    m = re.search(r"TOTAL PSS:\s*(\d+)", out) or re.search(r"^\s*TOTAL\s+(\d+)", out, re.M)
                    s["app_mem"] = round(int(m.group(1)) / 1024, 1) if m else None
            first = False
            self.dev.append(s)
            self.stop_ev.wait(interval)

    # -- screenshots taken in the headset become markers
    def _shots(self):
        seen = set()
        while not self.stop_ev.wait(3):
            _, out = shell(self.serial, f"find {SHOT_DIRS} -type f -newer {STAMP} 2>/dev/null", 15)
            for path in out.splitlines():
                path = path.strip()
                if not path.lower().endswith(IMG_EXT) or path in seen:
                    continue
                seen.add(path)
                t_seen = self.elapsed()
                time.sleep(1)  # let the headset finish writing the file
                fname = f"screenshots/headset_{len(seen):02d}_{os.path.basename(path)}"
                ok, _ = adb(self.serial, ["pull", path, os.path.join(self.folder, fname)], 60)
                m = self.add_marker("shot", "Screenshot taken in the headset", file=fname if ok else None)
                m["t"] = round(max(0, t_seen - 2), 2)  # the shot was taken a moment before we noticed it

    def screenshot(self):
        n = sum(1 for m in self.markers if m["kind"] == "shot") + 1
        fname = f"screenshots/pc_{n:02d}_{clock(self.elapsed()).replace(':', '-')}.png"
        try:
            res = subprocess.run([ADB, "-s", self.serial, "exec-out", "screencap", "-p"],
                                 capture_output=True, timeout=20, creationflags=NO_WINDOW)
        except Exception as e:
            return False, str(e)
        if not res.stdout.startswith(b"\x89PNG"):
            return False, "The headset did not return an image."
        with open(os.path.join(self.folder, fname), "wb") as f:
            f.write(res.stdout)
        self.add_marker("shot", "Screenshot from the PC", file=fname)
        return True, ""

    # -- video
    def _start_scrcpy(self):
        path = os.path.join(self.folder, "video.mkv")
        major = SCRCPY_VER[0] if SCRCPY_VER else 2
        if major >= 2:
            cmd = [SCRCPY, "-s", self.serial, "--no-playback", "--no-control", "--no-audio",
                   "--max-fps=30", "--video-bit-rate=8M", f"--record={path}"]
        else:
            cmd = [SCRCPY, "-s", self.serial, "--no-display", "--max-fps", "30", "--bit-rate", "8M", "--record", path]
        cmd += shlex.split(self.opts.get("video_args") or "")
        flags = NO_WINDOW | (subprocess.CREATE_NEW_PROCESS_GROUP if IS_WIN else 0)
        self.vproc = subprocess.Popen(cmd, stdout=PIPE, stderr=STDOUT, stdin=DEVNULL,
                                      env=dict(os.environ, ADB=ADB), creationflags=flags)
        self.video["parts"] = [{"file": "video.mkv", "offset": round(self.elapsed(), 2)}]
        self._thread(self._scrcpy_reader)

    def _scrcpy_reader(self):
        tail = deque(maxlen=12)
        with open(os.path.join(self.folder, "video_tool.log"), "w", encoding="utf-8") as f:
            for raw in iter(self.vproc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                f.write(line + "\n")
                tail.append(line)
                if "Recording started" in line:
                    self.video["parts"][0]["offset"] = round(self.elapsed(), 2)
        self.vproc.wait()
        if not self.stop_ev.is_set():
            self.video["error"] = "scrcpy stopped early: " + " | ".join(l for l in tail if l.strip())[-400:]
            self.add_marker("system", "Video recording stopped unexpectedly", auto=True)

    def _screenrecord(self):
        extra = self.opts.get("video_args") or ""
        if not re.fullmatch(r"[\w\s.=:-]*", extra):  # these go into a device shell: keep them simple
            extra = ""
        part = 0
        while not self.stop_ev.is_set():
            remote = f"/data/local/tmp/vrsr_{self.id}_{part:03d}.mp4"
            started = time.time()
            proc = subprocess.Popen([ADB, "-s", self.serial, "shell",
                                     f"screenrecord --time-limit 170 --bit-rate 8000000 {extra} {remote}"],
                                    stdout=PIPE, stderr=STDOUT, stdin=DEVNULL, creationflags=NO_WINDOW)
            self.video["parts"].append({"file": f"video_{part + 1:02d}.mp4",
                                        "offset": round(self.elapsed(), 2), "remote": remote})
            while proc.poll() is None:
                if self.stop_ev.wait(0.5):
                    shell(self.serial, "pkill -2 screenrecord", 10)  # SIGINT lets screenrecord close the mp4
                    try:
                        proc.wait(10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    break
            out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
            if not self.stop_ev.is_set() and time.time() - started < 5:
                self.video["error"] = "The headset recorder failed: " + (out.strip()[:300] or "no output")
                self.video["parts"].pop()
                break
            part += 1

    # -- stop and package
    def stop(self):
        with self.lock:
            if self.state != "recording":
                return
            self.state = "finalizing"
        self.t_end = time.time()
        self.status = "Stopping the capture…"
        self.stop_ev.set()
        if self.vproc and self.vproc.poll() is None:
            try:
                self.vproc.send_signal(signal.CTRL_BREAK_EVENT if IS_WIN else signal.SIGINT)
                self.vproc.wait(10)
            except Exception:
                pass
            if self.vproc.poll() is None:
                self.vproc.terminate()
                try:
                    self.vproc.wait(5)
                except subprocess.TimeoutExpired:
                    self.vproc.kill()
        if self.logproc and self.logproc.poll() is None:
            self.logproc.terminate()
        for t in self.threads:
            t.join(timeout=30)
        try:
            self.logf.close()
        except Exception:
            pass
        if self.video["mode"] == "screenrecord":
            parts = self.video["parts"]
            for i, p in enumerate(parts):
                self.status = f"Downloading video {i + 1}/{len(parts)} from the headset…"
                adb(self.serial, ["pull", p["remote"], os.path.join(self.folder, p["file"])], 900)
                shell(self.serial, f"rm -f {p['remote']}")
                p.pop("remote", None)
            self.video["parts"] = [p for p in parts if os.path.isfile(os.path.join(self.folder, p["file"]))]
        elif self.video["mode"] == "scrcpy" and not os.path.isfile(os.path.join(self.folder, "video.mkv")):
            self.video["parts"] = []
            self.video["error"] = self.video["error"] or "scrcpy did not produce a video file."
        shell(self.serial, f"rm -f {STAMP}")
        self.status = "Packaging the session…"
        try:
            write_outputs(self.folder, self.to_data())
            self.state = "done"
            self.status = "Session packaged."
        except Exception as e:
            self.state = "error"
            self.status = f"Packaging failed: {e}"

    def to_data(self):
        lines = self.lines
        markers = []
        for m in sorted(self.markers, key=lambda x: x["t"]):
            i = m["line"]
            a, b = max(0, i - 25), min(len(lines), i + 25)
            mm = {k: v for k, v in m.items()}
            mm["excerpt"], mm["hl"] = lines[a:b], i - a
            markers.append(mm)
        return {"format": 1, "tool_version": TOOL_VERSION, "id": self.name, "started": self.started_iso,
                "duration": round(self.elapsed(), 1), "device": self.device,
                "app": self.app or ({"pkg": self.pkg} if self.pkg else {}),
                "video": {"mode": self.video["mode"], "parts": self.video["parts"], "error": self.video["error"]},
                "counts": self.counts, "log_lines": len(lines), "markers": markers,
                "vr_metrics": self.vr, "device_metrics": self.dev, "top_errors": top_errors(lines),
                "bug": self.bug, "files": []}


# --------------------------------------------------------------------------- outputs
def top_errors(lines, limit=15):
    groups = {}
    for line in lines:
        m = LINE_RE.match(line)
        if not m or m.group(5) not in "EFA":
            continue
        tag, msg = m.group(6).strip(), m.group(7)
        key = (tag, NORM_RE.sub("#", msg)[:160])
        g = groups.get(key)
        if g:
            g["count"] += 1
        else:
            groups[key] = {"tag": tag, "level": "F" if m.group(5) in "FA" else "E",
                           "example": msg[:300], "count": 1, "first": m.group(2)}
    return sorted(groups.values(), key=lambda g: -g["count"])[:limit]


def write_csv(path, rows, cols):
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def fps_stats(vr):
    vals = [r["fps"] for r in vr if r.get("fps") is not None]
    if not vals:
        return None
    tgt = [r["target"] for r in vr if r.get("target")]
    target = max(set(tgt), key=tgt.count) if tgt else None
    below = sum(1 for r in vr if r.get("target") and r["fps"] < 0.85 * r["target"])
    return {"avg": round(sum(vals) / len(vals), 1), "min": min(vals), "target": target,
            "below_pct": round(100 * below / len(vals), 1) if target else None}


KIND_NAMES = {"manual": "Note", "crash": "Crash", "anr": "ANR", "exception": "Exception", "perf": "FPS drop",
              "shot": "Screenshot", "app": "App", "system": "System"}


def bug_markdown(d):
    b, dev, app = d.get("bug", {}), d.get("device", {}), d.get("app", {})
    esc = lambda s: str(s or "").replace("|", "\\|").replace("\n", " ")
    out = [f"# {b.get('title') or 'VR bug report'}", "",
           "| | |", "|---|---|",
           f"| Device | {esc(dev.get('manufacturer'))} {esc(dev.get('model'))} (build {esc(dev.get('build'))}, Android {esc(dev.get('android'))}) |"]
    if app.get("pkg"):
        out.append(f"| App | {esc(app.get('pkg'))} {esc(app.get('version_name'))} ({esc(app.get('version_code'))}) |")
    out += [f"| Date | {d.get('started', '').replace('T', ' ')} |", f"| Duration | {clock(d.get('duration', 0))} |"]
    if b.get("tester"):
        out.append(f"| Tester | {esc(b['tester'])} |")
    if b.get("severity"):
        out.append(f"| Severity | {esc(b['severity'])} |")
    for title, key in (("Steps to reproduce", "steps"), ("Expected result", "expected"), ("Actual result", "actual")):
        if b.get(key):
            out += ["", f"## {title}", b[key]]
    c = d.get("counts", {})
    out += ["", "## Session summary",
            f"- Errors: {c.get('E', 0)} · Fatal: {c.get('F', 0)} · Warnings: {c.get('W', 0)} · Log lines: {d.get('log_lines', 0)}"]
    fs = fps_stats(d.get("vr_metrics", []))
    if fs:
        line = f"- FPS: average {fs['avg']}, minimum {fs['min']:g}"
        if fs["target"]:
            line += f", target {fs['target']}, {fs['below_pct']}% of the time below 85% of target"
        out.append(line)
    if d.get("markers"):
        out += ["", "## Timeline", "| Time | Type | Note |", "|---|---|---|"]
        for m in d["markers"]:
            out.append(f"| {clock(m['t'])} | {KIND_NAMES.get(m['kind'], m['kind'])} | {esc(m['text'])} |")
    out += ["", "## Attached files",
            "- `report.html`: open it to watch the video with clickable markers, charts and the log around each marker",
            "- `logcat.log`: full log (open it in VR Log Inspector to filter it)",
            "- `vr_metrics.csv`, `device_metrics.csv`: performance data", ""]
    return "\n".join(out)


def make_zip(folder):
    zpath, tmp = folder + ".zip", folder + ".zip.part"
    base = os.path.basename(folder)
    with zipfile.ZipFile(tmp, "w") as z:
        for root, _, files in os.walk(folder):
            for fn in sorted(files):
                full = os.path.join(root, fn)
                arc = os.path.join(base, os.path.relpath(full, folder))
                stored = fn.lower().endswith((".mp4", ".mkv") + IMG_EXT)
                z.write(full, arc, compress_type=zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED)
    os.replace(tmp, zpath)


def write_outputs(folder, d):
    write_csv(os.path.join(folder, "vr_metrics.csv"), d.get("vr_metrics", []), VR_COLS)
    write_csv(os.path.join(folder, "device_metrics.csv"), d.get("device_metrics", []), DEV_COLS)
    with open(os.path.join(folder, "bug.md"), "w", encoding="utf-8") as f:
        f.write(bug_markdown(d))
    files = []
    for root, _, fs in os.walk(folder):
        for fn in fs:
            rel = os.path.relpath(os.path.join(root, fn), folder).replace("\\", "/")
            if rel not in ("session.json", "report.html"):
                files.append({"name": rel, "size": os.path.getsize(os.path.join(root, fn))})
    d["files"] = sorted(files, key=lambda x: x["name"])
    with open(os.path.join(folder, "session.json"), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    payload = json.dumps(d, ensure_ascii=False).replace("</", "<\\/")
    with open(os.path.join(folder, "report.html"), "w", encoding="utf-8") as f:
        f.write(REPORT.replace("__DATA__", payload))
    make_zip(folder)


def session_dir(sid):
    if not sid or not re.fullmatch(r"[\w.-]+", sid):
        return None
    p = os.path.join(OUT_DIR, sid)
    return p if os.path.isfile(os.path.join(p, "session.json")) else None


def load_session(sid):
    p = session_dir(sid)
    if not p:
        return None
    with open(os.path.join(p, "session.json"), encoding="utf-8") as f:
        return json.load(f)


def summary(d):
    return {"id": d["id"], "started": d.get("started", ""), "duration": d.get("duration", 0),
            "model": d.get("device", {}).get("model", ""), "pkg": d.get("app", {}).get("pkg", ""),
            "title": d.get("bug", {}).get("title", ""), "markers": len(d.get("markers", [])),
            "counts": d.get("counts", {}), "video": bool(d.get("video", {}).get("parts")),
            "video_error": d.get("video", {}).get("error", ""), "bug": d.get("bug", {}),
            "fps": fps_stats(d.get("vr_metrics", []))}


def list_sessions():
    out = []
    if not os.path.isdir(OUT_DIR):
        return out
    for name in sorted(os.listdir(OUT_DIR), reverse=True):
        if len(out) >= 40:
            break
        try:
            d = load_session(name)
        except Exception:
            d = None
        if d:
            out.append(summary(d))
    return out


# --------------------------------------------------------------------------- http
def state_json():
    s = CURRENT
    if not s:
        return {"state": "idle"}
    with s.lock:
        return {"state": s.state, "status": s.status, "elapsed": s.elapsed(), "id": s.name,
                "device": s.device, "app": s.app, "pkg": s.pkg, "counts": s.counts,
                "vr": s.vr[-1] if s.vr else None, "dev": s.dev[-1] if s.dev else None,
                "fps": [[x["t"], x["fps"], x.get("target")] for x in s.vr[-120:]],
                "markers": [{k: m[k] for k in ("id", "t", "kind", "text", "file")} for m in sorted(s.markers, key=lambda x: x["t"])],
                "lines": len(s.lines),
                "video": {"mode": s.video["mode"], "error": s.video["error"], "parts": len(s.video["parts"])}}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text", "application/json")) else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except ValueError:
            return {}

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/":
            return self._send(200, PAGE, "text/html")
        if url.path == "/api/env":
            ok, out = run([ADB, "version"], 10)
            return self._json({"adb": ADB, "adb_ok": ok, "scrcpy": SCRCPY,
                               "scrcpy_version": ".".join(map(str, SCRCPY_VER)) if SCRCPY_VER else "",
                               "out_dir": OUT_DIR})
        if url.path == "/api/devices":
            return self._json(list_devices())
        if url.path == "/api/apps":
            return self._json(list_apps(q.get("serial", "")))
        if url.path == "/api/state":
            return self._json(state_json())
        if url.path == "/api/sessions":
            return self._json({"sessions": list_sessions()})
        if url.path == "/api/session":
            d = load_session(q.get("id"))
            return self._json(summary(d) if d else {"error": "not found"}, 200 if d else 404)
        if url.path == "/download":
            p = session_dir(q.get("id"))
            z = p + ".zip" if p else None
            if not z or not os.path.isfile(z):
                return self._send(404, "not found", "text/plain")
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(os.path.getsize(z)))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(os.path.basename(z))}")
            self.end_headers()
            with open(z, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)
            return
        self._send(404, "not found", "text/plain")

    def do_POST(self):
        global CURRENT
        path = urlparse(self.path).path
        body = self._body()
        if path == "/api/start":
            with CURRENT_LOCK:
                if CURRENT and CURRENT.state in ("starting", "recording", "finalizing"):
                    return self._json({"ok": False, "error": "A session is already running."})
                pkg = (body.get("pkg") or "").strip()
                if pkg and not PKG_RE.match(pkg):
                    return self._json({"ok": False, "error": "That doesn't look like a package name (e.g. com.company.app)."})
                if not body.get("serial"):
                    return self._json({"ok": False, "error": "Pick a headset first."})
                CURRENT = Session(body["serial"], body)
                threading.Thread(target=CURRENT.start, daemon=True).start()
            return self._json({"ok": True})
        s = CURRENT
        if path == "/api/marker":
            if not s or s.state != "recording":
                return self._json({"ok": False, "error": "Not recording."})
            m = s.add_marker("manual", (body.get("text") or "").strip() or "Marked moment")
            return self._json({"ok": True, "marker": {k: m[k] for k in ("id", "t", "kind", "text")}})
        if path == "/api/screenshot":
            if not s or s.state != "recording":
                return self._json({"ok": False, "error": "Not recording."})
            ok, err = s.screenshot()
            return self._json({"ok": ok, "error": err})
        if path == "/api/stop":
            if s and s.state == "recording":
                threading.Thread(target=s.stop, daemon=True).start()
            return self._json({"ok": True})
        if path == "/api/reset":
            with CURRENT_LOCK:
                if s and s.state in ("done", "error"):
                    CURRENT = None
            return self._json({"ok": True})
        if path == "/api/bug":
            sid = body.get("id")
            d = load_session(sid)
            if not d:
                return self._json({"ok": False, "error": "Session not found."})
            bug = {k: str(body.get("bug", {}).get(k, ""))[:20000]
                   for k in ("title", "tester", "severity", "steps", "expected", "actual")}
            d["bug"] = bug
            if s and s.name == sid:
                s.bug = bug
            write_outputs(session_dir(sid), d)
            return self._json({"ok": True})
        if path == "/api/open":
            p = session_dir(body.get("id"))
            if not p:
                return self._json({"ok": False, "error": "Session not found."})
            try:
                open_path(os.path.join(p, "report.html") if body.get("what") == "report" else p)
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
        self._send(404, "not found", "text/plain")


def main():
    global ADB, SCRCPY, SCRCPY_VER, OUT_DIR
    ap = argparse.ArgumentParser(description="Record VR bug sessions: log + video + metrics + markers")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--adb", default=None, help="path to adb")
    ap.add_argument("--scrcpy", default=None, help="path to scrcpy")
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "vr_sessions"), help="where sessions are saved")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    ADB = find_adb(args.adb)
    SCRCPY, SCRCPY_VER = detect_scrcpy(args.scrcpy)
    OUT_DIR = os.path.abspath(args.out)
    os.makedirs(OUT_DIR, exist_ok=True)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"VR Session Recorder running at {url}")
    print(f"  adb:    {ADB}")
    print(f"  scrcpy: {SCRCPY or 'not found (video from the headset recorder only)'}")
    print(f"  saving sessions to {OUT_DIR}")
    print("Press Ctrl+C to quit.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if CURRENT and CURRENT.state == "recording":
            print("Packaging the running session before quitting…")
            CURRENT.stop()
            print(f"Saved to {CURRENT.folder}")
        server.server_close()


# --------------------------------------------------------------------------- shared look
BASE_CSS = r"""
:root{
  --bg:#141827; --panel:#1b2033; --panel-2:#22283f; --line:#2e3552;
  --text:#dde2f3; --muted:#8890ab; --accent:#8e9dff; --accent-ink:#10132a;
  --rec:#ff5d6c; --ok:#3fd38a;
  --c-unity:#4cc2ff; --c-xr:#c493ff;
  --l-e:#ff7070; --l-w:#f3bb52; --l-f:#ff5fb4;
  --mono: ui-monospace,"Cascadia Mono","JetBrains Mono",Menlo,Consolas,monospace;
  --ui: "Segoe UI Variable","Segoe UI",system-ui,-apple-system,Roboto,sans-serif;
  color-scheme: dark;
}
@media (prefers-color-scheme: light){
  :root{
    --bg:#eef0f7; --panel:#ffffff; --panel-2:#f4f5fb; --line:#d9dcea;
    --text:#1b2036; --muted:#646b86; --accent:#4655d6; --accent-ink:#ffffff;
    --rec:#d92c3c; --ok:#14935a;
    --c-unity:#0a8fd6; --c-xr:#8a4fe0;
    --l-e:#d23a3a; --l-w:#b57800; --l-f:#c4127a;
    color-scheme: light;
  }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 var(--ui)}
button,input,select,textarea{font:inherit;color:inherit}
button{background:var(--panel-2);border:1px solid var(--line);border-radius:7px;padding:6px 12px;cursor:pointer}
button:hover{border-color:var(--accent)}
button.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);font-weight:600}
button:disabled{opacity:.45;cursor:default}
input[type=text],select,textarea{background:var(--panel-2);border:1px solid var(--line);border-radius:7px;padding:7px 9px;min-width:0;width:100%}
textarea{resize:vertical;min-height:70px}
:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px}
.muted{color:var(--muted)}
.badge{display:inline-block;font-size:11.5px;font-weight:600;padding:1px 7px;border-radius:5px;border:1px solid currentColor;white-space:nowrap}
.k-manual{color:var(--accent)} .k-crash{color:var(--l-f)} .k-anr,.k-exception{color:var(--l-e)}
.k-perf{color:var(--l-w)} .k-shot{color:var(--c-unity)} .k-app{color:var(--c-xr)} .k-system{color:var(--muted)}
"""

# --------------------------------------------------------------------------- recorder UI
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VR Session Recorder</title>
<style>
""" + BASE_CSS + r"""
body{min-height:100vh}
header{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--line)}
header h1{font-size:15px;margin:0 8px 0 0;display:flex;gap:9px;align-items:center}
.reclogo{width:14px;height:14px;border-radius:50%;border:2px solid var(--rec);display:grid;place-items:center}
.reclogo::after{content:"";width:6px;height:6px;border-radius:50%;background:var(--rec)}
.grow{flex:1}
.pill{font-size:12px;padding:3px 9px;border-radius:99px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.pill.ok{color:var(--ok);border-color:currentColor} .pill.no{color:var(--l-w);border-color:currentColor}
main{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:16px;padding:16px;max-width:1400px;margin:0 auto}
@media (max-width:980px){main{grid-template-columns:1fr}}
h2{font-size:16px;margin:0 0 12px}
h3{font-size:13px;margin:18px 0 8px;color:var(--muted);font-weight:600}
label.f{display:block;font-size:12.5px;color:var(--muted);margin:12px 0 4px}
.row{display:flex;gap:8px;align-items:center}
.row > input{flex:1}
.opts{display:grid;gap:8px;margin-top:6px}
.opt{display:flex;gap:10px;align-items:flex-start;padding:10px;border:1px solid var(--line);border-radius:9px;cursor:pointer;background:var(--panel-2)}
.opt input{margin-top:3px}
.opt b{display:block;font-weight:600}
.opt span{font-size:12.5px;color:var(--muted)}
.opt.dis{opacity:.5;cursor:default}
.chk{display:flex;gap:8px;align-items:center;margin-top:8px;cursor:pointer}
.big{font-size:16px;padding:11px 22px;border-radius:10px}
.banner{padding:10px 12px;border-radius:9px;border:1px solid var(--l-e);color:var(--l-e);margin-bottom:12px}
.hidden{display:none !important}
code{font-family:var(--mono);font-size:12.5px;background:var(--panel-2);padding:1px 5px;border-radius:4px}
details summary{cursor:pointer;color:var(--muted);margin-top:14px}

/* recording */
.rechead{display:flex;flex-wrap:wrap;align-items:center;gap:14px}
.recpill{display:flex;align-items:center;gap:7px;font-weight:700;color:var(--rec);letter-spacing:.06em}
.recpill i{width:11px;height:11px;border-radius:50%;background:var(--rec);animation:blink 1.4s infinite}
@keyframes blink{50%{opacity:.25}}
@media (prefers-reduced-motion: reduce){.recpill i{animation:none}}
.timer{font:600 34px/1 var(--mono);font-variant-numeric:tabular-nums}
.who{color:var(--muted);font-size:13px}
.ctrl{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.ctrl input{flex:1;min-width:220px}
.stopbtn{background:var(--rec);border-color:var(--rec);color:#fff;font-weight:600}
.hint{font-size:12.5px;color:var(--muted);margin-top:8px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px;margin-top:16px}
.mc{background:var(--panel-2);border:1px solid var(--line);border-radius:9px;padding:9px 11px}
.mc .l{font-size:11.5px;color:var(--muted)}
.mc .v{font:600 20px/1.2 var(--mono);font-variant-numeric:tabular-nums;margin-top:2px}
.mc .v small{font-size:12px;color:var(--muted);font-weight:400}
.mc.warn .v{color:var(--l-w)} .mc.bad .v{color:var(--l-e)}
.spark{margin-top:12px;background:var(--panel-2);border:1px solid var(--line);border-radius:9px;padding:8px 10px}
.spark svg{width:100%;height:70px;display:block}
.mlist{margin-top:8px;display:flex;flex-direction:column;gap:2px;max-height:340px;overflow:auto}
.mi{display:grid;grid-template-columns:52px 92px 1fr;gap:8px;align-items:baseline;padding:5px 6px;border-radius:6px;font-size:13px}
.mi:nth-child(odd){background:var(--panel-2)}
.mi .t{font-family:var(--mono);color:var(--muted)}
.mi .x{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.spin{width:28px;height:28px;border:3px solid var(--line);border-top-color:var(--accent);border-radius:50%;animation:sp 1s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion: reduce){.spin{animation:none}}

/* done */
.sum{display:flex;flex-wrap:wrap;gap:8px;margin:4px 0 6px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}
@media (max-width:700px){.grid2{grid-template-columns:1fr}}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:16px}

/* history */
aside h2{display:flex;justify-content:space-between;align-items:center}
.sess{padding:10px;border:1px solid var(--line);border-radius:9px;margin-bottom:8px;cursor:pointer;background:var(--panel-2)}
.sess:hover{border-color:var(--accent)}
.sess b{display:block;font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sess .m{font-size:12px;color:var(--muted)}
.toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--text);color:var(--bg);padding:9px 15px;border-radius:9px;opacity:0;transition:opacity .2s;pointer-events:none;z-index:9;max-width:90vw}
.toast.show{opacity:1}
</style>
</head>
<body>
<header>
  <h1><span class="reclogo" aria-hidden="true"></span>VR Session Recorder</h1>
  <select id="device" style="width:auto;max-width:260px"><option value="">No headset found</option></select>
  <button id="rescan">Rescan</button>
  <span class="grow"></span>
  <span class="pill" id="pAdb">adb</span>
  <span class="pill" id="pScr">scrcpy</span>
</header>

<main>
<section>
  <!-- SETUP -->
  <div class="card" id="vSetup">
    <div class="banner hidden" id="err"></div>
    <h2>New session</h2>
    <label class="f" for="pkg">App under test</label>
    <div class="row">
      <input type="text" id="pkg" list="apps" placeholder="com.company.app (leave empty to record the whole headset)">
      <button id="detect" title="Use the app that is open in the headset right now">Detect open app</button>
    </div>
    <datalist id="apps"></datalist>
    <div class="grid2">
      <div><label class="f" for="title">Bug title (optional, can be filled in later)</label><input type="text" id="title"></div>
      <div><label class="f" for="tester">Tester</label><input type="text" id="tester"></div>
    </div>

    <h3>Video</h3>
    <div class="opts" id="vopts">
      <label class="opt" id="oScr"><input type="radio" name="video" value="scrcpy"><div><b>scrcpy (recommended)</b><span id="scrNote">Records on the PC with no time limit.</span></div></label>
      <label class="opt"><input type="radio" name="video" value="screenrecord"><div><b>Headset recorder</b><span>Uses the headset's own screenrecord in 170-second parts and downloads them at the end. Doesn't work on every firmware.</span></div></label>
      <label class="opt"><input type="radio" name="video" value="none"><div><b>No video</b><span>Only log, metrics and markers.</span></div></label>
    </div>

    <h3>Options</h3>
    <label class="chk"><input type="checkbox" id="clearLog"> Clear the headset log before starting</label>
    <label class="chk"><input type="checkbox" id="restart"> Restart the app when recording starts</label>
    <details>
      <summary>Advanced</summary>
      <label class="f" for="vargs">Extra video arguments</label>
      <input type="text" id="vargs" placeholder="e.g. --crop=1920:1440:20:350 --angle=20">
      <div class="hint">For scrcpy, cropping to one eye makes the video easier to watch. For the headset recorder, e.g. <code>--size 1920x1080</code>.</div>
      <label class="f" for="interval">Battery / temperature / memory sample every</label>
      <select id="interval" style="width:auto"><option value="2">2 s</option><option value="5">5 s</option><option value="10">10 s</option></select>
    </details>
    <div class="actions"><button class="primary big" id="start">● Start recording</button></div>
  </div>

  <!-- RECORDING -->
  <div class="card hidden" id="vRec">
    <div class="rechead">
      <span class="recpill"><i></i>REC</span>
      <span class="timer" id="timer">0:00</span>
      <span class="who" id="who"></span>
    </div>
    <div class="ctrl">
      <input type="text" id="note" placeholder="What just happened? Press Enter to mark this moment">
      <button id="mark" class="primary">Mark moment</button>
      <button id="shot">Screenshot</button>
      <button id="stop" class="stopbtn">■ Stop &amp; package</button>
    </div>
    <div class="hint">Wearing the headset? Take a screenshot with the controller: it is added as a marker automatically. Shortcuts here: <b>M</b> mark, <b>S</b> screenshot.</div>
    <div class="hint hidden" id="vidErr" style="color:var(--l-w)"></div>
    <div class="cards" id="cards"></div>
    <div class="spark"><div class="muted" style="font-size:12px">FPS · last 2 minutes</div><svg id="spark" viewBox="0 0 600 70" preserveAspectRatio="none"></svg></div>
    <h3>Markers</h3>
    <div class="mlist" id="mlist"><div class="muted" style="font-size:13px">Nothing yet. Crashes, ANRs, Unity exceptions and FPS drops are marked automatically.</div></div>
  </div>

  <!-- FINALIZING / STARTING -->
  <div class="card hidden" id="vBusy">
    <div class="row" style="gap:14px"><div class="spin"></div><div><b id="busyTitle">Working…</b><div class="muted" id="busyText"></div></div></div>
  </div>

  <!-- DONE -->
  <div class="card hidden" id="vDone">
    <h2 id="dTitle">Session packaged</h2>
    <div class="sum" id="dSum"></div>
    <div class="hint hidden" id="dVidErr" style="color:var(--l-w)"></div>
    <h3>Bug details (included in report.html and bug.md)</h3>
    <div class="grid2">
      <div><label class="f" for="bTitle">Title</label><input type="text" id="bTitle"></div>
      <div class="grid2" style="gap:0 10px">
        <div><label class="f" for="bSev">Severity</label>
          <select id="bSev"><option value="">–</option><option>Critical</option><option>High</option><option>Medium</option><option>Low</option></select></div>
        <div><label class="f" for="bTester">Tester</label><input type="text" id="bTester"></div>
      </div>
    </div>
    <label class="f" for="bSteps">Steps to reproduce</label><textarea id="bSteps"></textarea>
    <div class="grid2">
      <div><label class="f" for="bExp">Expected result</label><textarea id="bExp"></textarea></div>
      <div><label class="f" for="bAct">Actual result</label><textarea id="bAct"></textarea></div>
    </div>
    <div class="actions">
      <button class="primary" id="saveBug">Save details</button>
      <button id="dZip">Download .zip</button>
      <button id="dReport">Open report</button>
      <button id="dFolder">Open folder</button>
      <span class="grow"></span>
      <button id="newSess">New session</button>
    </div>
  </div>
</section>

<aside class="card">
  <h2>Recent sessions <button id="reloadS" title="Refresh">↻</button></h2>
  <div id="sessions"><div class="muted">No sessions yet.</div></div>
  <div class="hint" id="outDir"></div>
</aside>
</main>
<div class="toast" id="toast"></div>

<script>
"use strict";
const $ = id => document.getElementById(id);
const KIND = {manual:"Note",crash:"Crash",anr:"ANR",exception:"Exception",perf:"FPS drop",shot:"Screenshot",app:"App",system:"System"};
const STORE = "vrsessionrecorder.v1";
const prefs = Object.assign({video:"scrcpy", clearLog:true, restart:false, interval:"5", vargs:"", pkg:"", tester:"", serial:""},
  (()=>{ try { return JSON.parse(localStorage.getItem(STORE)||"{}"); } catch { return {}; } })());
const savePrefs = () => { try { localStorage.setItem(STORE, JSON.stringify(prefs)); } catch {} };
let env = {}, S = {state:"idle"}, doneId = null, lastState = "";

function esc(s){ return String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }
function clock(s){ s = Math.max(0, Math.floor(s)); const h = Math.floor(s/3600), m = Math.floor(s%3600/60), x = s%60; return (h ? h+":"+String(m).padStart(2,"0") : m) + ":" + String(x).padStart(2,"0"); }
let toastT; function toast(m){ const t=$("toast"); t.textContent=m; t.classList.add("show"); clearTimeout(toastT); toastT=setTimeout(()=>t.classList.remove("show"),3000); }
async function api(path, body){
  const r = await fetch(path, body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  return r.json();
}

// ---------------------------------------------------------------- setup
async function loadEnv(){
  env = await api("/api/env");
  $("pAdb").textContent = env.adb_ok ? "adb ✓" : "adb missing"; $("pAdb").className = "pill " + (env.adb_ok ? "ok" : "no");
  $("pScr").textContent = env.scrcpy ? "scrcpy " + (env.scrcpy_version || "✓") : "scrcpy missing";
  $("pScr").className = "pill " + (env.scrcpy ? "ok" : "no");
  const r = document.querySelector('input[value=scrcpy]');
  if(!env.scrcpy){
    r.disabled = true; $("oScr").classList.add("dis");
    $("scrNote").innerHTML = "Not installed. Install it with <code>winget install Genymobile.scrcpy</code> (Windows) or <code>brew install scrcpy</code> (macOS), then restart this tool.";
    if(prefs.video === "scrcpy") prefs.video = "screenrecord";
  }
  document.querySelector(`input[name=video][value=${prefs.video}]`).checked = true;
  $("outDir").textContent = "Saved in " + env.out_dir;
}
async function refreshDevices(){
  const sel = $("device");
  try {
    const j = await api("/api/devices");
    if(!j.ok){ sel.innerHTML = `<option value="">adb unavailable</option>`; return; }
    const ready = j.devices.filter(d => d.state === "device");
    sel.innerHTML = j.devices.length ? j.devices.map(d =>
      `<option value="${esc(d.serial)}" ${d.state!=="device"?"disabled":""}>${esc((d.model||"Device")+" · "+d.serial+(d.state!=="device"?" ("+d.state+")":""))}</option>`).join("")
      : `<option value="">No headset found</option>`;
    if(prefs.serial && ready.some(d => d.serial === prefs.serial)) sel.value = prefs.serial;
    else if(ready.length) sel.value = ready[0].serial;
    if(j.devices.some(d => d.state === "unauthorized")) toast("Put on the headset and accept USB debugging, then Rescan.");
    loadApps();
  } catch { toast("The recorder is not running. Start vr_session_recorder.py again."); }
}
async function loadApps(){
  const serial = $("device").value; if(!serial) return;
  try {
    const j = await api("/api/apps?serial=" + encodeURIComponent(serial));
    $("apps").innerHTML = j.apps.map(a => `<option value="${esc(a)}">`).join("");
    return j;
  } catch {}
}
$("detect").onclick = async () => {
  const j = await loadApps();
  if(j && j.foreground){ $("pkg").value = j.foreground; prefs.pkg = j.foreground; savePrefs(); toast("Using " + j.foreground); }
  else toast("Couldn't detect the open app. Open it in the headset and try again.");
};
$("rescan").onclick = refreshDevices;
$("device").onchange = () => { prefs.serial = $("device").value; savePrefs(); loadApps(); };

$("start").onclick = async () => {
  const video = document.querySelector("input[name=video]:checked").value;
  Object.assign(prefs, {video, pkg:$("pkg").value.trim(), tester:$("tester").value.trim(), clearLog:$("clearLog").checked,
    restart:$("restart").checked, interval:$("interval").value, vargs:$("vargs").value, serial:$("device").value});
  savePrefs();
  $("start").disabled = true;
  const j = await api("/api/start", {serial:prefs.serial, pkg:prefs.pkg, title:$("title").value.trim(), tester:prefs.tester,
    video, video_args:prefs.vargs, interval:prefs.interval, clear_log:prefs.clearLog, restart_app:prefs.restart});
  $("start").disabled = false;
  if(!j.ok){ toast(j.error || "Could not start"); return; }
  $("title").value = "";
  poll();
};

// ---------------------------------------------------------------- recording
async function mark(){
  const text = $("note").value;
  const j = await api("/api/marker", {text});
  if(j.ok){ $("note").value = ""; toast(`Marked at ${clock(j.marker.t)}`); poll(); }
}
async function shot(){
  $("shot").disabled = true;
  const j = await api("/api/screenshot", {});
  $("shot").disabled = false;
  toast(j.ok ? "Screenshot saved" : (j.error || "Screenshot failed") + " Tip: in VR apps PC screenshots can come out black; take it with the controller instead.");
  poll();
}
$("mark").onclick = mark;
$("shot").onclick = shot;
$("note").addEventListener("keydown", e => { if(e.key === "Enter") mark(); });
$("stop").onclick = async () => { $("stop").disabled = true; await api("/api/stop", {}); poll(); };
document.addEventListener("keydown", e => {
  if(S.state !== "recording" || /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName) || e.ctrlKey || e.metaKey || e.altKey) return;
  if(e.key === "m" || e.key === "M"){ e.preventDefault(); mark(); }
  if(e.key === "s" || e.key === "S"){ e.preventDefault(); shot(); }
});

function card(label, value, unit, cls){ return `<div class="mc ${cls||""}"><div class="l">${label}</div><div class="v">${value ?? "–"}${value != null && unit ? ` <small>${unit}</small>` : ""}</div></div>`; }
const THERMAL = ["none","light","moderate","severe","critical","emergency","shutdown"];
function renderRec(){
  $("timer").textContent = clock(S.elapsed);
  const d = S.device || {}, a = S.app || {};
  $("who").textContent = [d.model, d.build, a.pkg ? a.pkg + (a.version_name ? " " + a.version_name : "") : "whole headset"].filter(Boolean).join(" · ");
  const vr = S.vr || {}, dv = S.dev || {}, c = S.counts || {};
  const low = vr.target && vr.fps < 0.85 * vr.target;
  let h = "";
  h += card("FPS", vr.fps != null ? (vr.target ? `${vr.fps}<small>/${vr.target}</small>` : vr.fps) : null, "", low ? "bad" : "");
  if(vr.stale != null) h += card("Stale frames/s", vr.stale, "", vr.stale > 5 ? "warn" : "");
  if(vr.cpu_lvl != null) h += card("CPU / GPU level", `${vr.cpu_lvl}/${vr.gpu_lvl}`);
  if(vr.gpu_util != null) h += card("GPU load", Math.round(vr.gpu_util*100), "%", vr.gpu_util > .9 ? "warn" : "");
  if(vr.temp != null) h += card("Headset temp", vr.temp, "°C");
  h += card("Battery", dv.battery, "%", dv.battery != null && dv.battery < 20 ? "warn" : "");
  h += card("Battery temp", dv.bat_temp, "°C", dv.bat_temp > 42 ? "warn" : "");
  if(dv.thermal != null) h += card("Thermal", THERMAL[dv.thermal] || dv.thermal, "", dv.thermal >= 2 ? "bad" : dv.thermal === 1 ? "warn" : "");
  if(S.pkg) h += card("App memory", dv.app_mem, "MB", dv.pid === null ? "bad" : "");
  h += card("Errors / fatal", `${c.E||0}<small> / ${c.F||0}</small>`, "", c.F ? "bad" : "");
  h += card("Log lines", (S.lines||0).toLocaleString());
  $("cards").innerHTML = h;
  // sparkline
  const pts = S.fps || [], svg = $("spark");
  if(pts.length > 1){
    const t1 = pts[pts.length-1][0], t0 = t1 - 120;
    const tgt = Math.max(...pts.map(p => p[2] || 0));
    const top = Math.max(tgt, ...pts.map(p => p[1])) * 1.1 || 1;
    const x = t => (t - t0) / 120 * 600, y = v => 68 - v / top * 64;
    let s = tgt ? `<line x1="0" x2="600" y1="${y(tgt)}" y2="${y(tgt)}" stroke="var(--muted)" stroke-dasharray="4 4" stroke-width="1"/>` : "";
    s += `<path d="M${pts.map(p => x(p[0]).toFixed(1)+","+y(p[1]).toFixed(1)).join("L")}" fill="none" stroke="var(--accent)" stroke-width="2" vector-effect="non-scaling-stroke"/>`;
    svg.innerHTML = s;
  } else svg.innerHTML = `<text x="8" y="40" fill="var(--muted)" font-size="13">Waiting for FPS data (it comes from the VR runtime log while a VR app is running)…</text>`;
  // markers
  const ms = (S.markers || []).slice().reverse();
  if(ms.length) $("mlist").innerHTML = ms.map(m => `<div class="mi"><span class="t">${clock(m.t)}</span><span><span class="badge k-${m.kind}">${KIND[m.kind]||m.kind}</span></span><span class="x" title="${esc(m.text)}">${esc(m.text)}</span></div>`).join("");
  const ve = S.video && S.video.error;
  $("vidErr").classList.toggle("hidden", !ve); $("vidErr").textContent = ve ? "Video: " + ve : "";
}

// ---------------------------------------------------------------- done
function fillDone(s){
  doneId = s.id;
  const b = s.bug || {};
  $("dTitle").textContent = b.title || "Session packaged";
  const c = s.counts || {};
  const bits = [`<span class="pill">${clock(s.duration)}</span>`, `<span class="pill">${s.markers} markers</span>`,
    `<span class="pill">${c.E||0} errors · ${c.F||0} fatal</span>`, `<span class="pill">${s.video ? "video ✓" : "no video"}</span>`];
  if(s.fps) bits.push(`<span class="pill">FPS avg ${s.fps.avg}${s.fps.target ? " / " + s.fps.target : ""}</span>`);
  $("dSum").innerHTML = bits.join("");
  $("dVidErr").classList.toggle("hidden", !s.video_error); $("dVidErr").textContent = s.video_error ? "Video: " + s.video_error : "";
  $("bTitle").value = b.title || ""; $("bSev").value = b.severity || ""; $("bTester").value = b.tester || prefs.tester || "";
  $("bSteps").value = b.steps || ""; $("bExp").value = b.expected || ""; $("bAct").value = b.actual || "";
}
async function openDone(id){
  const s = await api("/api/session?id=" + encodeURIComponent(id));
  if(s.error){ toast("Session not found"); return; }
  fillDone(s); show("vDone");
}
$("saveBug").onclick = async () => {
  const bug = {title:$("bTitle").value, severity:$("bSev").value, tester:$("bTester").value, steps:$("bSteps").value, expected:$("bExp").value, actual:$("bAct").value};
  $("saveBug").disabled = true;
  const j = await api("/api/bug", {id:doneId, bug});
  $("saveBug").disabled = false;
  toast(j.ok ? "Saved in report.html, bug.md and the .zip" : j.error);
  if(j.ok){ $("dTitle").textContent = bug.title || "Session packaged"; loadSessions(); }
};
$("dZip").onclick = () => { location.href = "/download?id=" + encodeURIComponent(doneId); };
$("dReport").onclick = async () => { const j = await api("/api/open", {id:doneId, what:"report"}); if(!j.ok) toast(j.error); };
$("dFolder").onclick = async () => { const j = await api("/api/open", {id:doneId, what:"folder"}); if(!j.ok) toast(j.error); };
$("newSess").onclick = async () => { await api("/api/reset", {}); doneId = null; S = {state:"idle"}; show("vSetup"); poll(); };

// ---------------------------------------------------------------- history
async function loadSessions(){
  const j = await api("/api/sessions");
  $("sessions").innerHTML = j.sessions.length ? j.sessions.map(s => `<div class="sess" data-id="${esc(s.id)}" tabindex="0" role="button">
    <b>${esc(s.title || s.pkg || "Session")}</b>
    <div class="m">${esc(s.started.replace("T"," ").slice(0,16))} · ${clock(s.duration)} · ${s.markers} markers</div>
    <div class="m">${esc(s.model)}${s.pkg ? " · " + esc(s.pkg) : ""}</div></div>`).join("") : `<div class="muted">No sessions yet.</div>`;
}
$("sessions").addEventListener("click", e => { const el = e.target.closest(".sess"); if(el && !["starting","recording","finalizing"].includes(S.state)) openDone(el.dataset.id); });
$("sessions").addEventListener("keydown", e => { if(e.key === "Enter") e.target.closest(".sess")?.click(); });
$("reloadS").onclick = loadSessions;

// ---------------------------------------------------------------- state machine
function show(id){ ["vSetup","vRec","vBusy","vDone"].forEach(v => $(v).classList.toggle("hidden", v !== id)); }
let pollT;
async function poll(){
  clearTimeout(pollT);
  try { S = await api("/api/state"); } catch { S = {state:"offline"}; }
  const st = S.state;
  if(st === "recording"){ show("vRec"); $("stop").disabled = false; renderRec(); }
  else if(st === "starting" || st === "finalizing"){
    show("vBusy");
    $("busyTitle").textContent = st === "starting" ? "Starting…" : "Packaging the session…";
    $("busyText").textContent = S.status || "";
  }
  else if(st === "done"){
    if(lastState !== "done"){ await openDone(S.id); loadSessions(); }
  }
  else if(st === "error"){
    show("vSetup"); $("err").classList.remove("hidden"); $("err").textContent = S.status;
    await api("/api/reset", {});
  }
  else if(st === "offline"){ toast("Lost the connection to the recorder. Is it still running?"); }
  else if(lastState && lastState !== "idle" && st === "idle" && !doneId){ show("vSetup"); }
  if(st !== "error" && st !== "idle") $("err").classList.add("hidden");
  lastState = st;
  pollT = setTimeout(poll, ["recording","starting","finalizing"].includes(st) ? 1000 : 4000);
}

// ---------------------------------------------------------------- init
$("pkg").value = prefs.pkg; $("tester").value = prefs.tester; $("clearLog").checked = prefs.clearLog;
$("restart").checked = prefs.restart; $("interval").value = prefs.interval; $("vargs").value = prefs.vargs;
document.querySelectorAll("input[name=video]").forEach(r => r.onchange = () => { prefs.video = r.value; savePrefs(); });
(async () => { await loadEnv(); await refreshDevices(); loadSessions(); poll(); })();
</script>
</body>
</html>
"""

# --------------------------------------------------------------------------- report template
REPORT = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VR session report</title>
<style>
""" + BASE_CSS + r"""
.wrap{max-width:1180px;margin:0 auto;padding:20px 16px 60px}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:15px;margin:28px 0 10px}
.meta{color:var(--muted);font-size:13px}
.pills{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.pill{font-size:12.5px;padding:3px 10px;border-radius:99px;border:1px solid var(--line);background:var(--panel)}
.pill.bad{color:var(--l-e);border-color:currentColor}
.bug{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin-top:14px}
.bug .card h3{margin:0 0 6px;font-size:12.5px;color:var(--muted)}
.bug .card div{white-space:pre-wrap}
.player{display:grid;grid-template-columns:minmax(0,1.6fr) minmax(280px,1fr);gap:14px;align-items:start}
@media (max-width:900px){.player{grid-template-columns:1fr}}
video{width:100%;background:#000;border-radius:10px;max-height:62vh}
.vbar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:6px;font-size:12.5px;color:var(--muted)}
.vbar button{padding:2px 8px;font-size:12px}
.vbar button.on{border-color:var(--accent);color:var(--accent)}
.marks{max-height:62vh;overflow:auto;display:flex;flex-direction:column;gap:4px}
.mk{border:1px solid var(--line);border-radius:9px;background:var(--panel);padding:7px 9px}
.mk.cur{border-color:var(--accent)}
.mk .top{display:flex;gap:8px;align-items:center}
.mk .t{font-family:var(--mono);font-size:13px;padding:1px 7px}
.mk .x{margin-top:4px;font-size:13px;word-break:break-word}
.mk img{max-width:100%;border-radius:6px;margin-top:6px;display:block;cursor:zoom-in}
.mk .lg{font-size:12px;padding:1px 7px;margin-left:auto}
pre.log{margin:6px 0 0;padding:8px;background:var(--panel-2);border-radius:7px;font:11.5px/1.45 var(--mono);overflow:auto;max-height:320px;white-space:pre}
pre.log .hl{display:block;color:var(--accent);font-weight:700}
pre.log .e{color:var(--l-e)} pre.log .w{color:var(--l-w)} pre.log .f{color:var(--l-f)}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(440px,1fr));gap:12px}
@media (max-width:520px){.charts{grid-template-columns:1fr}}
.chart h3{margin:0 0 4px;font-size:13px}
.chart svg{width:100%;height:auto;display:block;cursor:crosshair}
.legend{display:flex;gap:12px;font-size:12px;color:var(--muted);margin-top:2px}
.legend i{display:inline-block;width:10px;height:3px;border-radius:2px;vertical-align:middle;margin-right:4px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px}
td.mono{font-family:var(--mono);font-size:12px;word-break:break-word}
.tw{overflow-x:auto}
.note{font-size:12.5px;color:var(--muted);margin-top:6px}
.lb{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;align-items:center;justify-content:center;z-index:5;cursor:zoom-out}
.lb img{max-width:95vw;max-height:95vh}
</style>
</head>
<body>
<div class="wrap" id="app"></div>
<div class="lb" id="lb"><img alt=""></div>
<script>
"use strict";
const D = __DATA__;
const KIND = {manual:"Note",crash:"Crash",anr:"ANR",exception:"Exception",perf:"FPS drop",shot:"Screenshot",app:"App",system:"System"};
const KCOL = {manual:"var(--accent)",crash:"var(--l-f)",anr:"var(--l-e)",exception:"var(--l-e)",perf:"var(--l-w)",shot:"var(--c-unity)",app:"var(--c-xr)",system:"var(--muted)"};
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const clock = s => { s = Math.max(0, Math.floor(s)); const h = Math.floor(s/3600), m = Math.floor(s%3600/60), x = s%60; return (h ? h+":"+String(m).padStart(2,"0") : m) + ":" + String(x).padStart(2,"0"); };
const size = n => n > 1e6 ? (n/1e6).toFixed(1)+" MB" : Math.max(1, Math.round(n/1e3))+" KB";
const dev = D.device || {}, app = D.app || {}, bug = D.bug || {}, c = D.counts || {};
const vr = D.vr_metrics || [], dm = D.device_metrics || [], parts = (D.video || {}).parts || [];

function fpsStats(){
  const v = vr.filter(r => r.fps != null); if(!v.length) return null;
  const avg = v.reduce((a,r) => a + r.fps, 0) / v.length, min = Math.min(...v.map(r => r.fps));
  const cnt = {}; v.forEach(r => { if(r.target) cnt[r.target] = (cnt[r.target]||0) + 1; });
  const keys = Object.keys(cnt), target = keys.length ? +keys.reduce((a,b) => cnt[a] >= cnt[b] ? a : b) : null;
  const below = v.filter(r => r.target && r.fps < .85*r.target).length;
  return {avg: avg.toFixed(1), min, target, below: target ? (100*below/v.length).toFixed(1) : null};
}
const fs = fpsStats();
document.title = (bug.title || "VR session") + " · " + (dev.model || "") ;

let h = `<h1>${esc(bug.title || "VR session report")}</h1>
<div class="meta">${esc([dev.manufacturer, dev.model].filter(Boolean).join(" "))} · build ${esc(dev.build)} · Android ${esc(dev.android)}
${app.pkg ? " · " + esc(app.pkg) + (app.version_name ? " " + esc(app.version_name) + " (" + esc(app.version_code) + ")" : "") : " · whole headset"}
· ${esc((D.started||"").replace("T"," "))}${bug.tester ? " · tester " + esc(bug.tester) : ""}</div>
<div class="pills"><span class="pill">${clock(D.duration)} recorded</span>
${bug.severity ? `<span class="pill ${/crit|high/i.test(bug.severity) ? "bad" : ""}">Severity: ${esc(bug.severity)}</span>` : ""}
<span class="pill ${c.F ? "bad" : ""}">${c.E||0} errors · ${c.F||0} fatal · ${c.W||0} warnings</span>
<span class="pill">${(D.markers||[]).length} markers</span>
${fs ? `<span class="pill ${fs.below > 5 ? "bad" : ""}">FPS avg ${fs.avg}${fs.target ? " / " + fs.target : ""} · min ${fs.min}${fs.below != null ? " · " + fs.below + "% below target" : ""}</span>` : ""}
</div>`;
if(bug.steps || bug.expected || bug.actual){
  h += `<div class="bug">` + [["Steps to reproduce", bug.steps], ["Expected result", bug.expected], ["Actual result", bug.actual]]
    .filter(x => x[1]).map(x => `<div class="card"><h3>${x[0]}</h3><div>${esc(x[1])}</div></div>`).join("") + `</div>`;
}
h += `<h2>Video and timeline</h2><div class="player"><div>`;
if(parts.length){
  h += `<video id="vid" controls preload="metadata"></video><div class="vbar"><span id="clk">Session time 0:00</span>` +
    (parts.length > 1 ? parts.map((p,i) => `<button data-part="${i}">Part ${i+1} · ${clock(p.offset)}</button>`).join("") : "") + `</div>
    <div class="note">Click a marker or a point on any chart to jump there. Video sync is approximate (±1–2 s). If the video doesn't play here, open ${esc(parts[0].file)} with VLC.</div>`;
} else {
  h += `<div class="card muted">No video in this session.${D.video && D.video.error ? " " + esc(D.video.error) : ""}</div>`;
}
h += `</div><div class="marks" id="marks"></div></div>`;
h += `<h2>Performance</h2><div class="charts" id="charts"></div><div class="note" id="noperf"></div>`;
h += `<h2>Most frequent errors</h2><div class="tw"><table><thead><tr><th>Count</th><th>Lvl</th><th>Tag</th><th>Message (example)</th><th>First seen</th></tr></thead><tbody>` +
  ((D.top_errors||[]).map(e => `<tr><td>${e.count}</td><td style="color:var(--l-${e.level.toLowerCase()})">${e.level}</td><td class="mono">${esc(e.tag)}</td><td class="mono">${esc(e.example)}</td><td class="mono">${esc(e.first)}</td></tr>`).join("")
   || `<tr><td colspan="5" class="muted">No errors in the log.</td></tr>`) + `</tbody></table></div>`;
h += `<h2>Files in this package</h2><div class="tw"><table><tbody>` + (D.files||[]).map(f => `<tr><td class="mono"><a href="${esc(f.name)}" style="color:var(--accent)">${esc(f.name)}</a></td><td>${size(f.size)}</td></tr>`).join("") + `</tbody></table></div>
<div class="note">logcat.log can be opened in VR Log Inspector to filter and search it. Generated by VR Session Recorder ${esc(D.tool_version)}.</div>`;
$("#app").innerHTML = h;

// ---------------------------------------------------------------- video sync
const vid = $("#vid"); let curPart = -1;
function loadPart(i){ if(i !== curPart){ curPart = i; vid.src = parts[i].file; document.querySelectorAll("[data-part]").forEach(b => b.classList.toggle("on", +b.dataset.part === i)); } }
function seek(t){
  if(!vid) return;
  let i = 0; parts.forEach((p,k) => { if(p.offset <= t) i = k; });
  loadPart(i);
  const go = () => { vid.currentTime = Math.max(0, t - parts[i].offset); vid.play().catch(()=>{}); };
  if(vid.readyState >= 1) go(); else vid.addEventListener("loadedmetadata", go, {once:true});
  vid.scrollIntoView({block:"nearest", behavior:"smooth"});
}
if(vid){
  loadPart(0);
  document.querySelectorAll("[data-part]").forEach(b => b.onclick = () => seek(parts[+b.dataset.part].offset));
  vid.addEventListener("timeupdate", () => {
    const t = parts[curPart].offset + vid.currentTime;
    $("#clk").textContent = "Session time " + clock(t);
    let best = null; (D.markers||[]).forEach(m => { if(m.t <= t + .5) best = m.id; });
    document.querySelectorAll(".mk").forEach(el => el.classList.toggle("cur", +el.dataset.id === best));
  });
  vid.addEventListener("ended", () => { if(curPart < parts.length - 1) seek(parts[curPart+1].offset); });
}

// ---------------------------------------------------------------- markers
function logHtml(m){
  return (m.excerpt||[]).map((l, i) => {
    const lv = (/^\S+\s+\S+\s+\d+\s+\d+\s+([VDIWEFA])\s/.exec(l) || [])[1];
    const cls = lv === "E" ? "e" : lv === "W" ? "w" : (lv === "F" || lv === "A") ? "f" : "";
    return (i === m.hl ? `<span class="hl">▶ marker: ${esc(KIND[m.kind]||m.kind)} at ${clock(m.t)}</span>` : "") + `<span class="${cls}">${esc(l)}</span>`;
  }).join("\n") + (m.hl >= (m.excerpt||[]).length ? `\n<span class="hl">▶ marker: ${esc(KIND[m.kind]||m.kind)} at ${clock(m.t)}</span>` : "");
}
const mk = (D.markers||[]);
$("#marks").innerHTML = mk.length ? mk.map(m => `<div class="mk" data-id="${m.id}">
  <div class="top"><button class="t" data-t="${m.t}">${clock(m.t)}</button><span class="badge k-${m.kind}">${KIND[m.kind]||m.kind}</span>
  ${(m.excerpt||[]).length ? `<button class="lg" data-log="${m.id}">Log</button>` : ""}</div>
  <div class="x">${esc(m.text)}</div>${m.file ? `<img src="${esc(m.file)}" alt="Screenshot at ${clock(m.t)}" loading="lazy">` : ""}
  <pre class="log" hidden id="log${m.id}">${logHtml(m)}</pre></div>`).join("")
  : `<div class="card muted">No markers in this session.</div>`;
$("#marks").addEventListener("click", e => {
  const t = e.target.closest("[data-t]"); if(t) seek(+t.dataset.t);
  const l = e.target.closest("[data-log]");
  if(l){ const p = document.getElementById("log" + l.dataset.log); p.hidden = !p.hidden; if(!p.hidden){ const hl = p.querySelector(".hl"); if(hl) p.scrollTop = hl.offsetTop - 60; } }
  const img = e.target.closest("img"); if(img){ $("#lb img").src = img.src; $("#lb").style.display = "flex"; }
});
$("#lb").onclick = () => { $("#lb").style.display = "none"; };

// ---------------------------------------------------------------- charts
function nice(x){ const p = Math.pow(10, Math.floor(Math.log10(x))), f = x/p; return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * p; }
function chart(title, series, o = {}){
  series = series.filter(s => s.points.length);
  if(!series.length) return;
  const all = series.flatMap(s => s.points.map(p => p[1]));
  const W = 900, H = 200, L = 46, R = 12, T = 10, B = 24;
  const dur = Math.max(D.duration || 1, 1);
  let lo = o.min ?? Math.min(...all), hi = o.max ?? Math.max(...all);
  if(o.min == null) lo = Math.min(lo, 0);
  if(hi === lo) hi = lo + 1;
  const step = nice((hi - lo) / 4); hi = Math.ceil(hi / step) * step; lo = Math.floor(lo / step) * step;
  const x = t => L + (W - L - R) * t / dur, y = v => T + (H - T - B) * (1 - (v - lo) / (hi - lo));
  let s = "";
  for(let v = lo; v <= hi + 1e-9; v += step){ s += `<line x1="${L}" x2="${W-R}" y1="${y(v)}" y2="${y(v)}" stroke="var(--line)"/><text x="${L-6}" y="${y(v)+4}" text-anchor="end" font-size="11" fill="var(--muted)">${+v.toFixed(2)}${o.unit||""}</text>`; }
  const ts = nice(dur / 8);
  for(let t = 0; t <= dur; t += ts){ s += `<text x="${x(t)}" y="${H-6}" text-anchor="middle" font-size="11" fill="var(--muted)">${clock(t)}</text>`; }
  (D.markers||[]).forEach(m => { s += `<line x1="${x(m.t)}" x2="${x(m.t)}" y1="${T}" y2="${H-B}" stroke="${KCOL[m.kind]||"var(--muted)"}" stroke-dasharray="3 3" opacity=".75"><title>${clock(m.t)} ${esc(KIND[m.kind]||"")}: ${esc(m.text)}</title></line>`; });
  series.forEach(se => {
    const d = se.points.map((p,i) => (i ? "L" : "M") + x(p[0]).toFixed(1) + "," + y(p[1]).toFixed(1)).join("");
    s += `<path d="${d}" fill="none" stroke="${se.color}" stroke-width="1.8" ${se.dash ? 'stroke-dasharray="5 4"' : ""}/>`;
  });
  const div = document.createElement("div"); div.className = "card chart";
  div.innerHTML = `<h3>${title}</h3><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}">${s}</svg>` +
    `<div class="legend">${series.map(se => `<span><i style="background:${se.color}"></i>${se.name}</span>`).join("")}</div>`;
  div.querySelector("svg").addEventListener("click", e => {
    const r = e.currentTarget.getBoundingClientRect(), px = (e.clientX - r.left) * W / r.width;
    seek(Math.max(0, (px - L) / (W - L - R) * dur));
  });
  $("#charts").appendChild(div);
}
const pts = (rows, k, f = v => v) => rows.filter(r => r[k] != null).map(r => [r.t, f(r[k])]);
chart("Frame rate", [{name:"FPS", color:"var(--accent)", points:pts(vr,"fps")}, {name:"Target", color:"var(--muted)", dash:true, points:pts(vr,"target")}]);
chart("Stale frames per second", [{name:"Stale", color:"var(--l-w)", points:pts(vr,"stale")}]);
chart("CPU / GPU load", [{name:"CPU", color:"var(--c-unity)", points:pts(vr,"cpu_util", v => v*100)}, {name:"GPU", color:"var(--c-xr)", points:pts(vr,"gpu_util", v => v*100)}], {min:0, max:100, unit:"%"});
chart("CPU / GPU clock level", [{name:"CPU level", color:"var(--c-unity)", points:pts(vr,"cpu_lvl")}, {name:"GPU level", color:"var(--c-xr)", points:pts(vr,"gpu_lvl")}], {min:0});
chart("Temperature", [{name:"Headset", color:"var(--l-e)", points:pts(vr,"temp")}, {name:"Battery", color:"var(--l-w)", points:pts(dm,"bat_temp")}], {unit:"°"});
chart("App memory (PSS)", [{name:"MB", color:"var(--accent)", points:pts(dm,"app_mem")}], {unit:""});
chart("Battery", [{name:"%", color:"var(--ok)", points:pts(dm,"battery")}], {min:0, max:100, unit:"%"});
chart("Thermal status (0 none … 4 critical)", [{name:"Status", color:"var(--l-e)", points:pts(dm,"thermal")}], {min:0, max:4});
if(!vr.length) $("#noperf").textContent = "No per-frame metrics: they come from the VR runtime log (VrApi on Meta Quest). Battery, temperature and memory are still sampled on any headset.";
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
