"""前端巡检: 逐页加载, 收集 JS 报错 / 失败请求 / 未绑定事件的按钮.

用法: python scripts/audit.py --base http://<host>:8890 --cookie mh_key=xxx
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shot import Cdp, find_chrome, free_port  # noqa: E402

ROUTES = [
    ("dashboard", "仪表盘"), ("downloads", "下载任务"), ("playlists", "歌单同步"),
    ("subscriptions", "歌单订阅"), ("library", "曲库统计"), ("repair", "库体检"),
    ("logs", "运行日志"), ("settings", "系统设置"),
]

AUDIT_JS = r"""
(() => {
  const out = [];
  document.querySelectorAll('button, a[href^="#/"], select, input[type=checkbox]').forEach(el => {
    const text = (el.innerText || el.value || el.getAttribute('placeholder') || '').trim().slice(0, 24);
    const bound = !!(el.onclick || el.onchange || el.oninput ||
                     (el.dataset && Object.keys(el.dataset).length));
    out.push({tag: el.tagName, text, bound,
              hidden: !(el.offsetWidth || el.offsetHeight)});
  });
  return JSON.stringify({title: document.querySelector('#pageTitle')?.innerText || '',
                         items: out});
})()
"""


def drain(cdp: Cdp, seconds: float) -> list[dict]:
    """收集这段时间内的事件(不阻塞)."""
    events = []
    cdp.ws.settimeout(0.25)
    end = time.time() + seconds
    while time.time() < end:
        try:
            msg = json.loads(cdp.ws.recv())
        except Exception:
            continue
        if "method" in msg:
            events.append(msg)
    cdp.ws.settimeout(30)
    return events


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://192.168.5.13:8890")
    ap.add_argument("--cookie", default="")
    ap.add_argument("--wait", type=float, default=2.6)
    args = ap.parse_args()

    port = free_port()
    profile = tempfile.mkdtemp(prefix="cdp-audit-")
    proc = subprocess.Popen([
        find_chrome(), "--headless=new", "--disable-gpu", "--no-sandbox",
        "--hide-scrollbars", f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}", "--window-size=1440,1000",
        "--remote-allow-origins=*", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    ws_url = ""
    for _ in range(80):
        try:
            data = json.load(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list", timeout=1))
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
    problems: list[str] = []
    try:
        cdp.send("Page.enable"); cdp.send("Runtime.enable")
        cdp.send("Log.enable"); cdp.send("Network.enable")
        cdp.send("Emulation.setDeviceMetricsOverride", width=1440, height=1000,
                 deviceScaleFactor=1, mobile=False)
        if args.cookie:
            name, _, value = args.cookie.partition("=")
            host = urlsplit(args.base).netloc
            cdp.send("Network.setCookie", name=name, value=value, path="/",
                     domain=host.split(":")[0])
        for route, label in ROUTES:
            cdp.send("Page.navigate", url=f"{args.base}/#/{route}")
            events = drain(cdp, args.wait)
            errors = []
            for ev in events:
                m = ev.get("method")
                p = ev.get("params") or {}
                if m == "Runtime.exceptionThrown":
                    errors.append("JS异常: " + str(p.get("exceptionDetails", {}).get("text"))[:120])
                elif m == "Runtime.consoleAPICalled" and p.get("type") == "error":
                    txt = " ".join(str(a.get("value", ""))[:80] for a in (p.get("args") or []))
                    errors.append("console.error: " + txt[:120])
                elif m == "Log.entryAdded":
                    e = p.get("entry") or {}
                    if e.get("level") in ("error",) and "favicon" not in str(e.get("url", "")):
                        errors.append(f"日志错误: {str(e.get('text'))[:110]}")
                elif m == "Network.responseReceived":
                    resp = p.get("response") or {}
                    if int(resp.get("status", 200)) >= 400:
                        errors.append(f"HTTP {resp.get('status')} {resp.get('url', '')[:90]}")
                elif m == "Network.loadingFailed":
                    errors.append(f"请求失败 {p.get('errorText')} {(p.get('requestId') or '')[:8]}")

            r = cdp.send("Runtime.evaluate", expression=AUDIT_JS, returnByValue=True)
            data = json.loads((r.get("result") or {}).get("value") or "{}")
            items = data.get("items") or []
            unbound = [i for i in items
                       if not i["bound"] and not i["hidden"] and i["tag"] in ("BUTTON", "SELECT")]
            print(f"\n== {label} ({route})  标题={data.get('title')!r}")
            print(f"   可交互元素 {len(items)} 个, 未绑定事件 {len(unbound)} 个")
            seen = set()
            for u in unbound:
                key = (u["tag"], u["text"])
                if key in seen:
                    continue
                seen.add(key)
                print(f"     · 未绑定: <{u['tag'].lower()}> {u['text']!r}")
                problems.append(f"{label}: 未绑定按钮 {u['text']!r}")
            if errors:
                uniq = list(dict.fromkeys(errors))[:6]
                for e in uniq:
                    print(f"     ! {e}")
                    problems.append(f"{label}: {e}")
            elif not unbound:
                print("     ✓ 无报错")
    finally:
        cdp.close()
        proc.kill()

    print("\n====== 汇总 ======")
    if problems:
        for p in dict.fromkeys(problems):
            print(" -", p)
        return 1
    print("未发现问题")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
