"""用 CDP 截图(支持登录态 Cookie), 仅用于开发期视觉验收.

用法:
  python scripts/shot.py --url http://host/page --out shot.png \
      --cookie session_id=xxx --width 1440 --height 1000 [--full]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import websocket

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]


def find_chrome() -> str:
    env = os.environ.get("CHROME_PATH")
    if env and Path(env).exists():
        return env
    for c in CHROME_CANDIDATES:
        if Path(c).exists():
            return c
    raise SystemExit("找不到 Chrome, 用 CHROME_PATH 指定")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Cdp:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=30,
                                              max_size=64 * 1024 * 1024)
        self.id = 0

    def send(self, method: str, **params) -> dict:
        self.id += 1
        self.ws.send(json.dumps({"id": self.id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--cookie", action="append", default=[])
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--wait", type=float, default=2.5)
    ap.add_argument("--click", default="", help="截图前点击的 CSS 选择器")
    args = ap.parse_args()

    port = free_port()
    profile = tempfile.mkdtemp(prefix="cdp-")
    proc = subprocess.Popen([
        find_chrome(), "--headless=new", "--disable-gpu", "--no-sandbox",
        "--hide-scrollbars", f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}", f"--window-size={args.width},{args.height}",
        "--remote-allow-origins=*",
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ws_url = ""
    for _ in range(60):
        try:
            data = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1))
            pages = [d for d in data if d.get("type") == "page"]
            if pages:
                ws_url = pages[0]["webSocketDebuggerUrl"]
                break
        except Exception:
            time.sleep(0.3)
    if not ws_url:
        proc.kill()
        raise SystemExit("CDP 未就绪")

    cdp = Cdp(ws_url)
    try:
        cdp.send("Page.enable")
        cdp.send("Network.enable")
        cdp.send("Emulation.setDeviceMetricsOverride", width=args.width, height=args.height,
                 deviceScaleFactor=1, mobile=False)
        for c in args.cookie:
            name, _, value = c.partition("=")
            cdp.send("Network.setCookie", name=name, value=value, path="/",
                     url=f"{urlsplit(args.url).scheme}://{urlsplit(args.url).netloc}")
        cdp.send("Page.navigate", url=args.url)
        time.sleep(args.wait)
        if args.click:
            cdp.send("Runtime.evaluate", expression=f"document.querySelector({args.click!r})?.click()")
            time.sleep(1.2)
        params = {"format": "png"}
        if args.full:
            params["captureBeyondViewport"] = True
        shot = cdp.send("Page.captureScreenshot", **params)
        Path(args.out).write_bytes(base64.b64decode(shot["data"]))
        print(f"saved {args.out}")
    finally:
        cdp.close()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        shutil.rmtree(profile, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
