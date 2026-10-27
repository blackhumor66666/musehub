"""QQ 音乐凭据诊断: 哪种 Cookie 组合 + 哪种 uin 能拿到音源."""
import json
import sys

import requests

from app.config import Config
from app.db import Database
from app import settings
from app.providers.registry import make_providers

cfg = Config.load()
db = Database(cfg.db_path)
settings.load_into(cfg, db)
p = make_providers(cfg, db)["qq"]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
SEARCH = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
PARAMS = {"ct": 24, "qqmusic_ver": 1298, "remoteplace": "txt.yqq.song", "t": 0,
          "aggr": 1, "cr": 1, "p": 1, "n": 3, "w": "晴天", "format": "json"}

full = p.cookies
keys_keep = ["qm_keyst", "qqmusic_key", "wxuin", "qqmusic_uin", "uin", "euin", "qlogin_uid"]
minimal = {k: full[k] for k in keys_keep if k in full}

variants = [("full", full), ("minimal", minimal), ("none", {})]
print("=== 搜索接口 ===")
for name, ck in variants:
    cookie_str = "; ".join(f"{k}={v}" for k, v in ck.items())
    try:
        r = requests.get(SEARCH, params=PARAMS, timeout=15,
                         headers={"User-Agent": UA, "Referer": "https://y.qq.com/",
                                  "Cookie": cookie_str} if ck else
                                 {"User-Agent": UA, "Referer": "https://y.qq.com/"})
        n = 0
        mid = ""
        if r.status_code == 200 and r.text.strip():
            try:
                d = r.json()
                lst = ((d.get("data") or {}).get("song") or {}).get("list") or []
                n = len(lst)
                mid = lst[0].get("songmid", "") if lst else ""
            except Exception:
                pass
        print(f"  {name:8s} http={r.status_code} songs={n} mid={mid} len={len(r.text)}")
    except Exception as exc:
        print(f"  {name:8s} ERR {exc}")

if not mid:
    mid = "0039MnYb0qxYhV"

print("\n=== vkey 接口 (songmid=%s) ===" % mid)
uins = [("0", 0), ("wxuin", full.get("wxuin", "")), ("qlogin_uid", full.get("qlogin_uid", ""))]
for label, uin in uins:
    for qname, prefix, ext in (("f000", "F000", "flac"), ("m800", "M800", "mp3")):
        body = {
            "req": {"module": "CDN.SrfCdnDispatchServer", "method": "GetCdnDispatch",
                    "param": {"guid": p._guid, "calltype": 0, "userip": ""}},
            "req_0": {"module": "vkey.GetVkeyServer", "method": "CgiGetVkey",
                      "param": {"guid": p._guid, "songmid": [mid], "songtype": [0],
                                "uin": str(uin), "loginflag": 1, "platform": "20",
                                "filename": [f"{prefix}{mid}{mid}.{ext}"]}},
            "comm": {"uin": int(uin) if str(uin).isdigit() else 0, "format": "json",
                     "ct": 24, "cv": 0},
        }
        try:
            r = requests.post("https://u.y.qq.com/cgi-bin/musicu.fcg", json=body, timeout=15,
                              headers={"User-Agent": UA, "Referer": "https://y.qq.com/"})
            d = r.json()
            info = (((d.get("req_0") or {}).get("data") or {}).get("midurlinfo") or [{}])[0]
            purl = info.get("purl") or ""
            print(f"  uin={label:10s} {qname}: http={r.status_code} purl={'YES' if purl else 'no'} "
                  f"code={(d.get('req_0') or {}).get('code')} err={info.get('result')}")
        except Exception as exc:
            print(f"  uin={label:10s} {qname}: ERR {exc}")
