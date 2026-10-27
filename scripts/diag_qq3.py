"""QQ vkey 验证(带 Cookie 的 session)."""
import json

from app.config import Config
from app.db import Database
from app import settings
from app.providers.registry import make_providers

cfg = Config.load()
db = Database(cfg.db_path)
settings.load_into(cfg, db)
p = make_providers(cfg, db)["qq"]

rows = db.query("SELECT title, artists, remote_id FROM tracks WHERE provider='qq' LIMIT 3")
uins = [("0", "0"), ("wxuin", p.cookies.get("wxuin", ""))]
platforms = ["20", "yqq.json"]

for r in rows:
    mid = r["remote_id"]
    print(f"--- {r['title']} / {mid} ---")
    for label, uin in uins:
        for plat in platforms:
            for qn, prefix, ext in (("f000", "F000", "flac"), ("m800", "M800", "mp3")):
                body = {
                    "req": {"module": "CDN.SrfCdnDispatchServer", "method": "GetCdnDispatch",
                            "param": {"guid": p._guid, "calltype": 0, "userip": ""}},
                    "req_0": {"module": "vkey.GetVkeyServer", "method": "CgiGetVkey",
                              "param": {"guid": p._guid, "songmid": [mid], "songtype": [0],
                                        "uin": str(uin), "loginflag": 1, "platform": plat,
                                        "filename": [f"{prefix}{mid}{mid}.{ext}"]}},
                    "comm": {"uin": int(uin) if str(uin).isdigit() else 0, "format": "json",
                             "ct": 24, "cv": 0},
                }
                try:
                    resp = p.session.post("https://u.y.qq.com/cgi-bin/musicu.fcg",
                                          data=json.dumps(body), timeout=15,
                                          headers={"Content-Type": "application/json"})
                    d = resp.json()
                    info = (((d.get("req_0") or {}).get("data") or {}).get("midurlinfo") or [{}])[0]
                    purl = info.get("purl") or ""
                    print(f"  uin={label:6s} plat={plat:9s} {qn}: http={resp.status_code} "
                          f"result={info.get('result')} purl={'YES' if purl else 'no'}"
                          + (f" url={purl[:50]}" if purl else ""))
                except Exception as exc:
                    print(f"  uin={label:6s} plat={plat:9s} {qn}: ERR {exc}")
