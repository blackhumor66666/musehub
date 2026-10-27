"""QQ vkey 深挖: 用真实歌单里的 songmid 试不同 uin / filename 组合."""
import json

import requests

from app.config import Config
from app.db import Database
from app import settings
from app.providers.registry import make_providers

cfg = Config.load()
db = Database(cfg.db_path)
settings.load_into(cfg, db)
p = make_providers(cfg, db)["qq"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

rows = db.query("SELECT title, artists, remote_id FROM tracks WHERE provider='qq' LIMIT 5")
print("候选曲目:")
for r in rows:
    print("  ", r["remote_id"], r["title"], "-", r["artists"])

print("\ncookie 形状:")
for k in ("uin", "wxuin", "qlogin_uid", "qqmusic_uin", "euin"):
    v = p.cookies.get(k)
    print(f"  {k:12s} = {v[:8] + '…' if v else '(无)'} len={len(v) if v else 0}")

candidates = [("0", "0"), ("wxuin", p.cookies.get("wxuin", "")),
              ("qlogin_uid", p.cookies.get("qlogin_uid", ""))]

def try_vkey(mid, uin, prefix, ext, platform="20"):
    body = {
        "req": {"module": "CDN.SrfCdnDispatchServer", "method": "GetCdnDispatch",
                "param": {"guid": p._guid, "calltype": 0, "userip": ""}},
        "req_0": {"module": "vkey.GetVkeyServer", "method": "CgiGetVkey",
                  "param": {"guid": p._guid, "songmid": [mid], "songtype": [0],
                            "uin": str(uin), "loginflag": 1, "platform": platform,
                            "filename": [f"{prefix}{mid}{mid}.{ext}"]}},
        "comm": {"uin": int(uin) if str(uin).isdigit() else 0, "format": "json",
                 "ct": 24, "cv": 0},
    }
    r = requests.post("https://u.y.qq.com/cgi-bin/musicu.fcg", json=body, timeout=15,
                      headers={"User-Agent": UA, "Referer": "https://y.qq.com/"})
    d = r.json()
    info = (((d.get("req_0") or {}).get("data") or {}).get("midurlinfo") or [{}])[0]
    return (d.get("req_0") or {}).get("code"), info.get("result"), info.get("purl") or ""

for r in rows[:3]:
    mid = r["remote_id"]
    print(f"\n--- {r['title']} / {mid} ---")
    for label, uin in candidates:
        for qn, prefix, ext in (("f000", "F000", "flac"), ("m800", "M800", "mp3")):
            try:
                code, res, purl = try_vkey(mid, uin, prefix, ext)
                print(f"  uin={label:11s} {qn}: code={code} result={res} purl={'YES' if purl else 'no'}")
            except Exception as exc:
                print(f"  uin={label:11s} {qn}: ERR {exc}")
