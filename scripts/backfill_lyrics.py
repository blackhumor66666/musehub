"""补齐歌词: 给已入库但缺 .lrc 的曲目抓歌词并落盘.

用法(容器内): python scripts/backfill_lyrics.py [--dry]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Config
from app.db import Database
from app.organizer import Organizer
from app.providers.base import RemoteTrack
from app.providers.registry import make_providers

dry = "--dry" in sys.argv

cfg = Config.load()
db = Database(cfg.db_path)
providers = make_providers(cfg, db)
org = Organizer(cfg.organizer)

rows = db.query(
    "SELECT provider, remote_id, title, artists, path FROM tracks "
    "WHERE status='active' AND path<>'' ORDER BY id"
)
print(f"候选 {len(rows)} 首")
done = skipped = failed = 0
for r in rows:
    f = cfg.music_dir / r["path"]
    if not f.exists():
        skipped += 1
        continue
    if f.with_suffix(".lrc").exists():
        skipped += 1
        continue
    p = providers.get(r["provider"])
    if p is None:
        skipped += 1
        continue
    if dry:
        print("  [dry]", r["path"])
        continue
    try:
        lyric = p.lyric(RemoteTrack(provider=r["provider"], remote_id=r["remote_id"],
                                    title=r["title"],
                                    artists=[a.strip() for a in (r["artists"] or "").split(";")]))
    except Exception:  # noqa: BLE001
        lyric = ""
    if not lyric:
        failed += 1
        continue
    org.save_lyric(f, lyric)
    done += 1

print(f"完成: 写入 {done} / 跳过 {skipped} / 无歌词 {failed}")
