"""把 quality='local'（旧版导入未识别品质）的记录, 按文件真实参数重算品质.

只改数据库里的品质标签, 不碰任何文件。跑一次即可:
  python /app/scripts/backfill_quality.py /music
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, "/app")

from app.config import Config  # noqa: E402
from app.db import Database  # noqa: E402
from app.library_import import audio_score  # noqa: E402


def main() -> int:
    cfg = Config.load()
    db = Database(cfg.db_path)
    rows = db.query(
        "SELECT id, path FROM tracks WHERE status='active' AND path<>'' AND quality='local'"
    )
    fixed = 0
    for r in rows:
        p = cfg.music_dir / r["path"]
        _score, q = audio_score(p)
        if q:
            db.execute("UPDATE tracks SET quality=? WHERE id=?", (q, r["id"]))
            fixed += 1
    print(f"回填完成: {fixed}/{len(rows)} 条记录的品质已按真实参数重算")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
