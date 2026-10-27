"""补齐封面: 给已入库但没封面的曲目下载封面并写入文件/目录.

用法(容器内): python scripts/backfill_cover.py [--dry]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Config
from app.db import Database
from app.downloader import Downloader
from app.organizer import Organizer
from app.providers.registry import make_providers

dry = "--dry" in sys.argv

cfg = Config.load()
db = Database(cfg.db_path)
providers = make_providers(cfg, db)
org = Organizer(cfg.organizer)
dl = Downloader(cfg, db, providers)

rows = db.query(
    "SELECT id, path, title, artists, cover FROM tracks "
    "WHERE status='active' AND path<>'' AND cover<>'' ORDER BY id"
)
print(f"候选 {len(rows)} 首")
done = skipped = failed = 0
for r in rows:
    f = cfg.music_dir / r["path"]
    if not f.exists():
        skipped += 1
        continue
    try:
        from mutagen import File as MFile
        a = MFile(str(f))
        has = bool(getattr(a, "pictures", None)) if a else False
        if not has and hasattr(a, "tags") and a.tags:
            has = any(k.startswith("APIC") or k == "covr" for k in a.tags.keys())
        if has:
            skipped += 1
            continue
    except Exception:
        pass
    if dry:
        print("  [dry]", r["path"])
        continue
    img = dl.fetch_image(r["cover"])
    if not img:
        failed += 1
        print("  封面下载失败:", r["title"])
        continue
    org.embed_cover(f, img)
    org.save_cover_file(f, img)
    done += 1
    print("  ✓", r["path"])

print(f"完成: 写入 {done} / 跳过 {skipped} / 失败 {failed}")
