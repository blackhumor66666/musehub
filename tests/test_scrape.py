"""元数据刮削: 只补空缺、按平台 ID 精确取、不覆盖已有值."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mutagen import File as MF

from app import scrape
from app.config import Config
from app.db import Database
from app.downloader import Downloader
from app.navidrome import Navidrome
from app.organizer import Organizer
from app.providers.registry import build_mock_providers
from app.sync import SyncEngine


class _Ctx:
    def __init__(self, cfg, db, providers):
        self.cfg = cfg
        self.db = db
        self.providers = providers


class TestScrape(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="musehub-scrape-"))
        os.environ["DATA_DIR"] = str(self.tmp / "data")
        os.environ["MUSIC_DIR"] = str(self.tmp / "music")
        os.environ["DRY_RUN"] = "0"
        os.environ["NAVIDROME_URL"] = ""
        os.environ["MUSEHUB_MOCK"] = "1"
        self.cfg = Config.load(env_file=self.tmp / "none.env")
        self.db = Database(self.cfg.db_path)
        self.db.kv_set("dry_run", "0")
        self.providers = build_mock_providers(self.cfg, self.db)
        self.engine = SyncEngine(
            self.cfg, self.db, self.providers,
            Downloader(self.cfg, self.db, self.providers),
            Organizer(self.cfg.organizer), Navidrome(self.cfg.navidrome, self.db),
        )
        self.ne = self.providers["netease"]
        # 先正常下一首, 拿到真实文件
        self.ne.seed_favorites(["101"])
        pid = self.db.upsert_playlist("netease", "fav", "我喜欢的音乐")
        self.db.execute("UPDATE playlists SET subscribed=1 WHERE id=?", (pid,))
        self.engine.run(trigger="test")
        self.row = self.db.find_track("netease", "101")
        self.assertEqual(self.row["status"], "active", self.row)

    def tearDown(self) -> None:
        for p in self.providers.values():
            p.stop()
        self.db.close()

    def test_scan_reports_missing_fields(self) -> None:
        # 人为清空年份/音轨号, 模拟"平台当时没给"
        self.db.execute("UPDATE tracks SET year='', track_no=0 WHERE id=?", (self.row["id"],))
        res = scrape.scan(self.db)
        self.assertEqual(res["counts"]["year"], 1, res)
        self.assertEqual(res["counts"]["track_no"], 1, res)
        self.assertEqual(res["exact"], 1, res)
        self.assertEqual(res["search_only"], 0, res)

    def test_run_fills_only_missing(self) -> None:
        self.db.execute("UPDATE tracks SET year='', track_no=0 WHERE id=?", (self.row["id"],))
        before_title = self.row["title"]
        before_album = self.row["album"]
        stats = scrape.run(_Ctx(self.cfg, self.db, self.providers), write_files=True)
        self.assertEqual(stats["updated"], 1, stats)
        self.assertEqual(stats["by_field"]["year"], 1, stats)

        row = self.db.get_track(self.row["id"])
        self.assertEqual(row["year"], "2003", row)      # mock 目录里 晴天 = 2003
        self.assertEqual(int(row["track_no"]), 1, row)
        # 已有值不该被动
        self.assertEqual(row["title"], before_title)
        self.assertEqual(row["album"], before_album)

        # 文件标签也应同步(ID3 用 TDRC, FLAC 用 date)
        f = self.cfg.music_dir / row["path"]
        tags = MF(str(f)).tags or {}
        year_tag = tags.get("date") or tags.get("TDRC")
        self.assertTrue(year_tag, f"文件里应写入年份, 实际标签: {list(tags)}")
        self.assertEqual(str(year_tag[0])[:4], "2003")

    def test_dry_run_touches_nothing(self) -> None:
        self.db.execute("UPDATE tracks SET year='' WHERE id=?", (self.row["id"],))
        stats = scrape.run(_Ctx(self.cfg, self.db, self.providers), dry_run=True)
        self.assertEqual(stats["updated"], 1, stats)     # 统计上算"可补"
        self.assertEqual(stats["files_written"], 0, stats)
        self.assertEqual(self.db.get_track(self.row["id"])["year"], "", "试运行不能写库")


    def test_sync_must_not_wipe_scraped_fields(self) -> None:
        """同步歌单时平台列表缺字段, 不能用空值覆盖已补全的年份/音轨号."""
        self.db.execute("UPDATE tracks SET year='2016', track_no=6 WHERE id=?",
                        (self.row["id"],))
        # 模拟下一次同步: 平台列表里 year/track_no 是空的
        self.db.upsert_track({
            "provider": "netease", "remote_id": "101", "title": self.row["title"],
            "artists": self.row["artists"], "album": "", "albumartist": "",
            "year": "", "track_no": 0, "cover": "", "duration": 0,
            "path": self.row["path"], "status": "active",
        })
        row = self.db.get_track(self.row["id"])
        self.assertEqual(row["year"], "2016", f"年份被空值覆盖了: {row}")
        self.assertEqual(int(row["track_no"]), 6, f"音轨号被覆盖了: {row}")
        self.assertEqual(row["album"], self.row["album"], "专辑被空值覆盖了")


    def test_parallel_fetch_returns_every_id(self) -> None:
        """并发取详情: 每条都要有结果, 且进度要推进(不能漏任务)."""
        import os
        from types import SimpleNamespace

        class FakeProvider:
            def __init__(self):
                self.cfg = SimpleNamespace()
                self.db = None
                self.seen = 0

            def track_detail(self, rid):
                time.sleep(0.01)
                return {"id": rid}

        base = FakeProvider()
        tasks = [({"id": i, "remote_id": str(i), "title": f"t{i}", "artists": "a"},
                  ["year"], base) for i in range(1, 31)]
        progress = {}
        old = os.environ.get("SCRAPE_WORKERS")
        os.environ["SCRAPE_WORKERS"] = "4"
        try:
            out = scrape._fetch_details(tasks, progress)
        finally:
            if old is None:
                os.environ.pop("SCRAPE_WORKERS", None)
            else:
                os.environ["SCRAPE_WORKERS"] = old
        self.assertEqual(len(out), 30, f"漏了任务: {len(out)}/30")
        self.assertEqual(progress.get("done"), 30, progress)
        self.assertEqual(progress.get("total"), 30, progress)

    def test_local_tracks_are_skipped(self) -> None:
        tid = self.db.upsert_track({
            "provider": "local", "remote_id": "本地/无ID.flac", "title": "无ID",
            "artists": "某人", "path": "本地/无ID.flac", "managed": 0, "status": "active",
        })
        stats = scrape.run(_Ctx(self.cfg, self.db, self.providers))
        self.assertGreaterEqual(stats["no_id"], 1, stats)
        self.assertEqual(self.db.get_track(tid)["year"], "", "本地文件没有平台 ID, 不该被改")


if __name__ == "__main__":
    unittest.main()
