"""用 CDP 打开页面并打印 JS 异常 / console 错误, 用于排查前端问题.

用法: python scripts/probe.py --url http://host/#/downloads [--cookie k=v]
"""
from __future__ import annotations

import argparse
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
]


def find_chrome() -> str:
    env = os.environ.get("CHROME_PATH")
    if env and Path(env).exists():
        return env
    for c in CHROME_CANDIDATES:
        if Path(c).exists():
            return c
    raise SystemExit("找不到 Chrome")


def free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--cookie", action="append", default=[])
    ap.add_argument("--wait", type=float, default=5.0)
    ap.add_argument("--eval", default="", help="额外执行的 JS 表达式")
    args = ap.parse_args()

    port = free_port()
    profile = tempfile.mkdtemp(prefix="cdp-")
    proc = subprocess.Popen([find_chrome(), "--headless=new", "--disable-gpu", "--no-sandbox",
                             f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                             "--remote-allow-origins=*", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ws_url = ""
    for _ in range(60):
        try:
            data = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1))
            pages = [d for d in data if d.get("type") == "page"]
            if pages:
                ws_url = pages[0]["webSocketDebuggerUrl"]; break
        except Exception:
            time.sleep(0.3)
    if not ws_url:
        proc.kill(); raise SystemExit("CDP 未就绪")

    ws = websocket.create_connection(ws_url, timeout=30, max_size=32 * 1024 * 1024)
    mid = 0

    def send(method, **params):
        nonlocal mid
        mid += 1
        ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == mid:
                return msg.get("result", {})
            handle_event(msg)

    errors: list[str] = []

    def handle_event(msg):
        m = msg.get("method")
        if m == "Runtime.exceptionThrown":
            d = msg["params"]["exceptionDetails"]
            txt = d.get("exception", {}).get("description") or d.get("text")
            errors.append(f"[exception] {txt}")
        elif m == "Runtime.consoleAPICalled" and msg["params"]["type"] == "error":
            parts = [str(a.get("value", a.get("description", ""))) for a in msg["params"]["args"]]
            errors.append("[console.error] " + " ".join(parts))
        elif m == "Log.entryAdded" and msg["params"]["entry"]["level"] == "error":
            errors.append("[log] " + msg["params"]["entry"]["text"])
        elif m == "Network.responseReceived":
            resp = msg["params"]["response"]
            if resp.get("status", 0) >= 400:
                errors.append(f"[http {resp['status']}] {resp.get('url','')[:160]}")
        elif m == "Network.loadingFailed":
            errors.append(f"[net fail] {msg['params'].get('errorText','')}")

    try:
        send("Runtime.enable")
        send("Log.enable")
        send("Page.enable")
        send("Network.enable")
        for c in args.cookie:
            n, _, v = c.partition("=")
            send("Network.setCookie", name=n, value=v, path="/",
                 url=f"{urlsplit(args.url).scheme}://{urlsplit(args.url).netloc}")
        send("Page.navigate", url=args.url)
        time.sleep(args.wait)
        if args.eval:
            r = send("Runtime.evaluate", expression=args.eval, returnByValue=True, awaitPromise=True)
            print("EVAL:", json.dumps(r.get("result", {}).get("value"), ensure_ascii=False)[:2000])
        # 抓一下页面里是否出现"加载失败"
        r = send("Runtime.evaluate", expression="document.querySelector('#page')?.innerText.slice(0,300)",
                 returnByValue=True)
        print("PAGE TEXT:", (r.get("result", {}).get("value") or "").replace("\n", " | ")[:400])
    finally:
        ws.close(); proc.terminate()
        try: proc.wait(timeout=5)
        except Exception: proc.kill()
        shutil.rmtree(profile, ignore_errors=True)

    if errors:
        print("\n=== JS 错误 ===")
        for e in dict.fromkeys(errors):
            print(" -", e[:500])
        return 1
    print("\n无 JS 错误")
    return 0


if __name__ == "__main__":
    sys.exit(main())
