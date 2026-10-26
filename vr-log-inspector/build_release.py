#!/usr/bin/env python3
"""
Build the release packages of VR Log Inspector.

    python build_release.py               -> dist/VR_Log_Inspector_<version>.zip
    python build_release.py --portable    -> also dist/VR_Log_Inspector_<version>_portable_win64.zip

The normal package holds the script, the launchers and the README.
The portable package (Windows) also includes the official "embeddable" Python from
python.org, so it runs on a PC without Python installed: unzip and double-click.

It also writes dist/latest.json, which a copy of the tool reads to find out about new versions.
Publish it next to the normal zip wherever the update source points (see --update-source).

    --update-source SRC  build the packages so they look for updates in SRC:
                         a folder (shared drive or synced OneDrive/Google Drive/SharePoint folder),
                         an https URL of latest.json, or github:OWNER/REPO (releases of this repo)

Options for --portable:
    --python 3.14.8     Python version to embed (it must have an embeddable package on python.org)
    --arch amd64        amd64 (default) or arm64
    --include-adb       also put Google's platform-tools inside the package, for PCs without
                        internet. By default the tool downloads adb itself on first use.
Run it from the folder that contains vr_log_inspector.py. Needs internet only for --portable.
"""

import argparse
import datetime
import hashlib
import json
import io
import os
import re
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = "VR Log Inspector"
COMMON = ["vr_log_inspector.py", "Start VR Log Inspector.bat", "README.txt"]
UNIX_LAUNCHERS = ["Start VR Log Inspector.command", "start_vr_log_inspector.sh"]
PY_URL = os.environ.get("VRLI_PY_URL", "https://www.python.org/ftp/python/{ver}/python-{ver}-embed-{arch}.zip")
ADB_URL = os.environ.get("VRLI_ADB_URL", "https://dl.google.com/android/repository/platform-tools-latest-windows.zip")


def tool_version():
    src = open(os.path.join(HERE, "vr_log_inspector.py"), encoding="utf-8").read()
    m = re.search(r'^TOOL_VERSION = "([^"]+)"', src, re.M)
    if not m:
        sys.exit("Couldn't read TOOL_VERSION from vr_log_inspector.py")
    return m.group(1)


def add_bytes(z, arcname, data, executable=False):
    info = zipfile.ZipInfo(arcname, date_time=(2026, 1, 1, 0, 0, 0))
    info.external_attr = (0o100755 if executable else 0o100644) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    z.writestr(info, data)


def add_local(z, name, executable=False):
    path = os.path.join(HERE, name)
    if not os.path.isfile(path):
        sys.exit(f"Missing file: {name}")
    add_bytes(z, f"{TOP}/{name}", open(path, "rb").read(), executable)


def changelog_notes(ver):
    """The CHANGELOG.md section of this version, shown in the tool's update notice."""
    for path in (os.path.join(HERE, "CHANGELOG.md"), os.path.join(HERE, "..", "CHANGELOG.md")):
        if os.path.isfile(path):
            text = open(path, encoding="utf-8").read()
            part = text.split("## VR Log Inspector", 1)[-1].split("\n## ", 1)[0]
            m = re.search(r"^### " + re.escape(ver) + r"\b.*?(?=^### |\Z)", part, re.M | re.S)
            if m:
                return m.group(0).strip()
    return ""


def download(url, label):
    print(f"Downloading {label}\n  {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "VR-Log-Inspector-build"})
    with urllib.request.urlopen(req, timeout=120) as r:
        total, buf = int(r.headers.get("Content-Length") or 0), io.BytesIO()
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            buf.write(chunk)
            if total:
                print(f"\r  {buf.tell() / 1048576:5.1f} / {total / 1048576:.1f} MB", end="", flush=True)
    data = buf.getvalue()
    print(f"\r  {len(data) / 1048576:.1f} MB, sha256 {hashlib.sha256(data).hexdigest()}")
    return data


def copy_zip(z, data, prefix, must_have):
    with zipfile.ZipFile(io.BytesIO(data)) as src:
        names = src.namelist()
        for pattern in must_have:
            if not any(re.fullmatch(pattern, n) for n in names):
                sys.exit(f"The download doesn't look right: nothing matches {pattern}")
        for info in src.infolist():
            if info.is_dir():
                continue
            add_bytes(z, prefix + info.filename, src.read(info))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--portable", action="store_true", help="also build the Windows package with Python included")
    ap.add_argument("--python", default="3.14.8", help="Python version to embed (default 3.14.8)")
    ap.add_argument("--arch", default="amd64", choices=["amd64", "arm64"])
    ap.add_argument("--include-adb", action="store_true", help="put platform-tools inside the portable package")
    ap.add_argument("--out", default=os.path.join(HERE, "dist"))
    ap.add_argument("--update-source", default="", help="where the packaged tool looks for updates")
    a = ap.parse_args()

    ver = tool_version()
    os.makedirs(a.out, exist_ok=True)

    update_cfg = json.dumps({"source": a.update_source}, indent=2).encode() if a.update_source else None
    normal = os.path.join(a.out, f"VR_Log_Inspector_{ver}.zip")
    with zipfile.ZipFile(normal, "w") as z:
        for name in COMMON:
            add_local(z, name)
        for name in UNIX_LAUNCHERS:
            add_local(z, name, executable=True)
        if update_cfg:
            add_bytes(z, f"{TOP}/update.json", update_cfg)
    print(f"Built {normal}")
    data = open(normal, "rb").read()
    manifest = {"tool": "vr-log-inspector", "version": ver, "file": os.path.basename(normal),
                "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
                "published": datetime.date.today().isoformat(), "notes": changelog_notes(ver)}
    with open(os.path.join(a.out, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"Built {os.path.join(a.out, 'latest.json')}")

    if a.portable:
        py = download(PY_URL.format(ver=a.python, arch=a.arch), f"Python {a.python} embeddable ({a.arch}) from python.org")
        adb = download(ADB_URL, "platform-tools for Windows from Google") if a.include_adb else None
        suffix = "win64" if a.arch == "amd64" else "winarm64"
        portable = os.path.join(a.out, f"VR_Log_Inspector_{ver}_portable_{suffix}.zip")
        with zipfile.ZipFile(portable, "w") as z:
            for name in COMMON:
                add_local(z, name)
            copy_zip(z, py, f"{TOP}/python/", [r"python\.exe", r"python3\d+\._pth"])
            if update_cfg:
                add_bytes(z, f"{TOP}/update.json", update_cfg)
            if adb:
                copy_zip(z, adb, f"{TOP}/", [r"platform-tools/adb\.exe"])
        print(f"Built {portable}")
        print("Test it on a PC without Python: unzip, double-click 'Start VR Log Inspector.bat'.")


if __name__ == "__main__":
    main()
