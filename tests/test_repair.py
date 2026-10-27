"""库体检(缺封面/缺歌词)的检测口径与修复行为.

重点验证:
  - 有内嵌封面/同目录 cover.jpg 时不误报
  - 真缺的时候能查出来
  - 修复只补缺的, 不覆盖已有的
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import repair
from app.config import Config
from app.db import Database
from app.downloader import Downloader
from app.navidrome import Navidrome
from app.organizer import Organizer
from app.providers.registry import build_mock_providers
from app.sync import SyncEngine


class _Ctx:
    """repair.fix 只需要这几样东西, 用最小外壳代替完整运行时."""

    def __init__(self, cfg, db, providers, downloader):
        self.cfg = cfg
        self.db = db
        self.providers = providers
        self.downloader = downloader


class TestRepair(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="musehub-repair-"))
        os.environ["DATA_DIR"] = str(self.tmp / "data")
        os.environ["MUSIC_DIR"] = str(self.tmp / "music")
        os.environ["DRY_RUN"] = "0"
        os.environ["NAVIDROME_URL"] = ""
        os.environ["MUSEHUB_MOCK"] = "1"
        self.cfg = Config.load(env_file=self.tmp / "none.env")
        self.db = Database(self.cfg.db_path)
        self.db.kv_set("dry_run", "0")
        self.providers = build_mock_providers(self.cfg, self.db)
        self.dl = Downloader(self.cfg, self.db, self.providers)
        self.engine = SyncEngine(
            self.cfg, self.db, self.providers, self.dl,
            Organizer(self.cfg.organizer), Navidrome(self.cfg.navidrome, self.db),
        )
        self.ne = self.providers["netease"]

    def tearDown(self) -> None:
        for p in self.providers.values():
            p.stop()
        self.db.close()

    def _bare_wav(self, rel: str) -> Path:
        """造一个没有任何标签/封面/歌词的 wav."""
        import wave as _wave
        p = self.cfg.music_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with _wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\x00" * 2000)
        return p

    def test_scan_detects_missing_cover_and_lyric(self) -> None:
        # 1) 走正常下载链路: 应内嵌封面 + 写 cover.jpg, 只缺歌词(mock 不提供歌词)
        self.ne.seed_favorites(["101"])
        pid = self.db.upsert_playlist("netease", "fav", "我喜欢的音乐")
        self.db.execute("UPDATE playlists SET subscribed=1 WHERE id=?", (pid,))
        self.engine.run(trigger="test")
        row = self.db.find_track("netease", "101")
        self.assertEqual(row["status"], "active", row)

        res = repair.scan(self.db, self.cfg)
        self.assertEqual(res["checked"], 1, res)
        self.assertEqual(res["missing_cover"], 0, "内嵌封面/cover.jpg 不该被报缺失")
        self.assertEqual(res["missing_lyric"], 1, "mock 没有歌词, 应报缺歌词")
        self.assertEqual(res["items"][0]["missing"], ["lyric"])

        # 2) 再塞一个"裸文件"(用户自己放的): 封面和歌词都缺
        rel = "裸文件/无名 - 测试.wav"
        self._bare_wav(rel)
        self.db.upsert_track({
            "provider": "local", "remote_id": rel, "title": "测试", "artists": "无名",
            "path": rel, "managed": 0, "status": "active", "source": "local",
        })
        res2 = repair.scan(self.db, self.cfg)
        self.assertEqual(res2["checked"], 2, res2)
        self.assertEqual(res2["missing_cover"], 1, res2)
        self.assertEqual(res2["missing_lyric"], 2, res2)
        self.assertEqual(res2["both"], 1, res2)

    def test_fix_only_touches_missing_parts(self) -> None:
        """修复只补缺的: 已经有的封面不能被动, 拿不到歌词算"无源"而不是失败."""
        self.ne.seed_favorites(["101"])
        pid = self.db.upsert_playlist("netease", "fav", "我喜欢的音乐")
        self.db.execute("UPDATE playlists SET subscribed=1 WHERE id=?", (pid,))
        self.engine.run(trigger="test")
        row = self.db.find_track("netease", "101")
        audio = self.cfg.music_dir / row["path"]
        cover_before = (audio.parent / "cover.jpg")
        self.assertTrue(cover_before.exists(), "下载后应有 cover.jpg")
        mtime = cover_before.stat().st_mtime

        ctx = _Ctx(self.cfg, self.db, self.providers, self.dl)
        out = repair.fix(ctx, ids=[row["id"]], kinds=["cover", "lyric"])
        self.assertEqual(out["fixed_cover"], 0, "已有封面不该重复写")
        self.assertEqual(out["fixed_lyric"], 0, out)      # mock 没有歌词
        self.assertGreaterEqual(out["no_source"], 1, out)
        self.assertEqual(cover_before.stat().st_mtime, mtime, "封面文件不该被重写")


if __name__ == "__main__":
    unittest.main()
