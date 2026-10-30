"""端到端流程测试: 订阅 -> 下载 -> 归档 -> 取消收藏 -> 本地删除回推.

全部离线运行, 用 MockProvider 真起 HTTP 服务吐音频, 走真实的
下载/校验/打标签/移动链路.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Config
from app.db import Database
from app.downloader import Downloader
from app.navidrome import Navidrome
from app.organizer import Organizer, sanitize_component
from app.providers.registry import build_mock_providers
from app.sync import SyncEngine


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="musehub-test-"))
        os.environ["DATA_DIR"] = str(self.tmp / "data")
        os.environ["MUSIC_DIR"] = str(self.tmp / "music")
        os.environ["DRY_RUN"] = "0"
        os.environ["DELETE_POLICY"] = "managed_only"
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

    def tearDown(self) -> None:
        for p in self.providers.values():
            p.stop()
        self.db.close()

    def subscribe(self, remote_id: str = "fav") -> int:
        return self.db.upsert_playlist("netease", remote_id, "我喜欢的音乐")

    def audio_files(self) -> list[Path]:
        return sorted(p for p in (self.cfg.music_dir).rglob("*") if p.suffix == ".wav")

    def write_wav(self, rel: str, rate: int = 8000, width: int = 2) -> Path:
        """造一个指定采样率/位宽的 wav, 用来构造"不同音质版本"的场景."""
        import wave as _wave
        p = self.cfg.music_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with _wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(width)
            w.setframerate(rate)
            w.writeframes(b"\x00" * (rate // 8) * width)
        return p


class TestSyncFlow(BaseCase):
    # ---------- 搜歌下载与平台收藏的关系 ----------

    def test_quality_upgrade_replaces_lossy(self) -> None:
        """洗版: 有损记录重下后应换成无损, 且只换真的更好的."""
        self.ne.seed_favorites(["101"])
        self.subscribe()
        self.engine.run(trigger="test")
        row = self.db.find_track("netease", "101")
        self.assertTrue(row["quality"], "应该记录了音质档位")

        # 人为把它降级成有损, 模拟"库里有损需要洗版"
        self.db.execute("UPDATE tracks SET quality='m800' WHERE id=?", (row["id"],))
        # mock provider 拿到的是整份 Config, 这里补上音质链(真实 provider 拿的是平台段)
        self.ne.cfg = self.cfg.netease
        lossy = self.engine.lossy_tracks()
        self.assertEqual(len(lossy), 1, lossy)

        stats = self.engine.upgrade_quality()
        self.assertEqual(stats["upgraded"], 1, stats)
        row2 = self.db.find_track("netease", "101")
        self.assertNotEqual(row2["quality"], "m800", "洗版后不该还是有损档位")
        from app.quality import is_lossless
        self.assertTrue(is_lossless(row2["quality"]), row2["quality"])
        self.assertEqual(self.engine.lossy_tracks(), [], "洗版后不该再出现在有损清单")

    def test_quality_upgrade_skips_when_already_best(self) -> None:
        """重新下一遍结果一样时不能白搬文件(记录会被标成"已是最佳")."""
        self.ne.seed_favorites(["101"])
        self.subscribe()
        self.engine.run(trigger="test")
        row = self.db.find_track("netease", "101")
        self.db.execute("UPDATE tracks SET quality='m800' WHERE id=?", (row["id"],))
        # 目标就定 320k: 已经是 320k 了, 不该再动文件
        self.ne.cfg = self.cfg.netease
        before = self.db.find_track("netease", "101")
        stats = self.engine.upgrade_quality(target="exhigh")
        self.assertEqual(stats["upgraded"], 0, stats)
        self.assertGreaterEqual(stats["skipped"], 1, stats)
        after = self.db.find_track("netease", "101")
        self.assertEqual(before["path"], after["path"], "没升级就不该动文件")

    def test_search_track_is_independent_from_favorites(self) -> None:
        """搜歌入库的曲目不属于任何歌单: 删文件不该回推取消收藏, 失败记录也不该被清掉."""
        hits = self.ne.search("晴天", limit=5)
        self.assertTrue(hits)
        track = hits[0]
        res = self.engine.download_single(track)
        self.assertTrue(res.get("ok"), res)

        row = self.db.find_track("netease", str(track.remote_id))
        self.assertEqual(row["source"], "search", row)

        # 用户删掉这个文件 -> 只标记 missing, 不产生 unfavorite 动作
        (self.cfg.music_dir / row["path"]).unlink()
        stats = self.engine.run(trigger="test")
        self.assertEqual(stats["missing"], 1, stats)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM actions", default=0), 0,
                         "搜歌入库的曲目与平台收藏无关, 不该回推取消收藏")
        self.assertEqual(self.db.get_track(row["id"])["status"], "missing")

        # 再跑几轮也不会把它当"孤儿记录"删掉(它不是歌单成员)
        self.engine.run(trigger="test")
        self.assertIsNotNone(self.db.get_track(row["id"]))

    def test_search_failed_record_survives_orphan_cleanup(self) -> None:
        """搜歌失败时记录要留着(用户能看原因/重试), 不能被孤儿清理抹掉."""
        tid = self.db.upsert_track({
            "provider": "netease", "remote_id": "999999", "title": "不存在的歌",
            "artists": "无人", "status": "failed", "fail_reason": "无音源",
            "source": "search",
        })
        self.engine.run(trigger="test")
        self.assertIsNotNone(self.db.get_track(tid), "source=search 的失败记录不应被清理")
        # 对照: 不属于任何歌单的普通失败记录仍然会被清掉
        tid2 = self.db.upsert_track({
            "provider": "netease", "remote_id": "999998", "title": "残留",
            "artists": "无人", "status": "failed", "source": "",
        })
        self.engine.run(trigger="test")
        self.assertIsNone(self.db.get_track(tid2))

    def test_search_download_can_write_back_favorite(self) -> None:
        """打开「搜歌下载后自动收藏」时, 下载完要真的在平台收藏里."""
        self.cfg.sync.favorite_on_download = True
        hits = self.ne.search("孤勇者", limit=3)
        self.assertTrue(hits)
        track = hits[0]
        res = self.engine.download_single(track)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res.get("favorited"), "已收藏", res)
        self.assertIn(str(track.remote_id), self.ne.favorites())

    # ---------- 专辑 / 榜单订阅 ----------
    def test_album_subscription(self) -> None:
        """专辑订阅: 按 kind=album 拉取, 曲目能正常下载入库."""
        pid = self.db.upsert_playlist("netease", "al_hairui", "叶惠美", kind="album")
        self.db.execute("UPDATE playlists SET subscribed=1 WHERE id=?", (pid,))
        stats = self.engine.run(trigger="test")
        self.assertEqual(stats["playlist_errors"], 0, stats)
        self.assertGreaterEqual(stats["downloaded"], 1, stats)
        row = self.db.query_one("SELECT kind, track_count FROM playlists WHERE id=?", (pid,))
        self.assertEqual(row["kind"], "album")

    def test_chart_is_additive_only(self) -> None:
        """榜单是单向来源: 内容换血后, 旧歌不删文件、不回推取消收藏, 新歌照常下载."""
        pid = self.db.upsert_playlist("netease", "26", "热歌榜", kind="chart")
        self.db.execute("UPDATE playlists SET subscribed=1 WHERE id=?", (pid,))
        stats = self.engine.run(trigger="test")
        self.assertEqual(stats["downloaded"], 2, stats)
        self.assertEqual(len(self.audio_files()), 2)

        # 榜单换血: 原来的 101/102 跌出, 新歌 103 上榜
        self.ne._chart_members["26"] = ["103"]
        stats = self.engine.run(trigger="test")
        self.assertEqual(stats["downloaded"], 1, stats)
        self.assertEqual(stats["deleted"], 0, stats)
        self.assertEqual(stats["removed_from_playlist"], 0, stats)
        # 旧歌文件还在, 新歌也下好了
        self.assertEqual(len(self.audio_files()), 3, [str(f) for f in self.audio_files()])
        # 不该有任何取消收藏动作
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM actions", default=0), 0)
        # 播放器镜像歌单保留全部 3 首(旧歌不缩水)
        items = self.db.playlist_track_ids(pid)
        self.assertEqual(len(items), 3, items)

    def test_chart_subscription(self) -> None:
        pid = self.db.upsert_playlist("netease", "26", "热歌榜", kind="chart")
        self.db.execute("UPDATE playlists SET subscribed=1 WHERE id=?", (pid,))
        stats = self.engine.run(trigger="test")
        self.assertEqual(stats["playlist_errors"], 0, stats)
        self.assertEqual(stats["downloaded"], 2, stats)
        self.assertEqual(self.db.query_one("SELECT kind FROM playlists WHERE id=?", (pid,))["kind"],
                         "chart")

    def test_full_cycle(self) -> None:
        self.ne.seed_favorites(["101", "102", "104"])
        pid = self.subscribe()

        stats = self.engine.run(trigger="test")
        self.assertEqual(stats["downloaded"], 3, stats)
        files = self.audio_files()
        self.assertEqual(len(files), 3, [str(f) for f in files])

        # 命名规范: 多艺人只取第一艺人做目录, 不出现 "David GuettaSia"
        rel = sorted(str(f.relative_to(self.cfg.music_dir)).replace("\\", "/") for f in files)
        self.assertIn("周杰伦/叶惠美/周杰伦 - 晴天.wav", rel)
        self.assertIn("David Guetta/Nothing But The Beat/David Guetta - Titanium.wav", rel)
        # 专辑名==歌名 -> 归到单曲目录
        self.assertIn("陈奕迅/单曲/陈奕迅 - 孤勇者.wav", rel)

        # 标签写对了
        from mutagen.wave import WAVE
        t = WAVE(str(self.cfg.music_dir / "David Guetta" / "Nothing But The Beat" / "David Guetta - Titanium.wav"))
        self.assertEqual(t.tags["TIT2"].text[0], "Titanium")
        self.assertEqual(t.tags["TPE1"].text[0], "David Guetta; Sia")
        self.assertEqual(t.tags["TPE2"].text[0], "David Guetta")

        # 增量: 新增收藏
        self.ne.add_tracks("fav", ["103"])
        stats = self.engine.run(trigger="test")
        self.assertEqual(stats["downloaded"], 1, stats)
        self.assertEqual(len(self.audio_files()), 4)

        # 取消收藏 -> 本地文件被清理
        self.ne.remove_tracks("fav", ["101"])
        stats = self.engine.run(trigger="test")
        self.assertEqual(stats["deleted"], 1, stats)
        self.assertFalse(any("晴天" in str(f) for f in self.audio_files()))
        row = self.db.find_track("netease", "101")
        self.assertEqual(row["status"], "deleted")

        # 用户手动删文件 -> 自动取消平台收藏
        victim = self.cfg.music_dir / "David Guetta" / "Nothing But The Beat" / "David Guetta - Titanium.wav"
        victim.unlink()
        stats = self.engine.run(trigger="test")
        self.assertEqual(stats["missing"], 1, stats)
        self.assertGreaterEqual(stats["actions_done"], 1, stats)
        self.assertNotIn("102", self.ne.favorites())
        self.assertEqual(self.db.find_track("netease", "102")["status"], "missing")

        # 恢复文件 -> 状态自动回滚
        stats = self.engine.run(trigger="test")
        self.assertEqual(stats["restored"], 0, "文件已被删, 不应恢复")
        del pid

    def test_local_delete_pushes_unfavorite(self) -> None:
        """反向链路逐项验证: 本地删文件 -> 排队 -> 回推成功 -> 不被复活.

        test_full_cycle 已经覆盖了"动作发生了", 这里再钉死几个细节:
        动作的状态是 done、reason 是 local_file_deleted、下一轮不会被歌单重新拉起来。
        生产库 actions 表长期为空, 是因为上线后没人手动删过文件(0 条 missing),
        不是链路不通。
        """
        self.ne.seed_favorites(["101"])
        self.subscribe()
        self.engine.run(trigger="test")
        row = self.db.find_track("netease", "101")
        self.assertEqual(row["status"], "active", row)
        self.assertIn("101", self.ne.favorites())

        # 模拟用户在 SMB / 播放器里把文件删了
        (self.cfg.music_dir / row["path"]).unlink()

        stats = self.engine.run(trigger="test")
        self.assertEqual(stats["missing"], 1, stats)
        self.assertEqual(stats["actions_done"], 1, stats)

        acts = [dict(r) for r in self.db.query("SELECT * FROM actions")]
        self.assertEqual(len(acts), 1, acts)
        self.assertEqual(acts[0]["kind"], "unfavorite")
        self.assertEqual(acts[0]["state"], "done", acts)
        payload = acts[0]["payload"]
        if isinstance(payload, str):
            import json as _json
            payload = _json.loads(payload)
        self.assertEqual(payload["reason"], "local_file_deleted")
        self.assertNotIn("101", self.ne.favorites(), "平台侧应已取消收藏")

        # 下一轮不能被"歌单里还有它"重新拉起来下载
        stats = self.engine.run(trigger="test")
        self.assertEqual(stats.get("downloaded", 0), 0, stats)
        self.assertEqual(self.db.get_track(row["id"])["status"], "missing")

    def test_manual_delete_removes_file_and_unfavorites(self) -> None:
        """面板里手动删歌: 删文件 + 标记 + 立刻回推取消收藏."""
        self.ne.seed_favorites(["101"])
        self.subscribe()
        self.engine.run(trigger="test")
        row = self.db.find_track("netease", "101")
        src = self.cfg.music_dir / row["path"]
        self.assertTrue(src.exists())

        r = self.engine.delete_track(row["id"])
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r["files"], 1, r)
        self.assertGreaterEqual(r["queued"], 1, r)
        self.assertFalse(src.exists(), "文件应已删除")
        self.assertEqual(self.db.get_track(row["id"])["status"], "deleted")
        self.assertNotIn("101", self.ne.favorites(), "应立即回推取消收藏")

        # 再删一次: 状态已不是 active, 应拒绝而不是报错崩掉
        r2 = self.engine.delete_track(row["id"])
        self.assertFalse(r2.get("ok"))
        self.assertIn("状态", r2.get("error", ""))

    def test_manual_delete_blocked_in_dry_run(self) -> None:
        self.ne.seed_favorites(["101"])
        self.subscribe()
        self.engine.run(trigger="test")
        row = self.db.find_track("netease", "101")
        self.db.kv_set("dry_run", "1")
        self.cfg.sync.dry_run = True
        r = self.engine.delete_track(row["id"])
        self.assertFalse(r.get("ok"))
        self.assertTrue((self.cfg.music_dir / row["path"]).exists(), "试运行不能删文件")

    def test_local_delete_keeps_favorite_when_another_copy_exists(self) -> None:
        """同一首歌还有别的文件时不取消收藏, 但要通知"删了一个副本"."""
        self.ne.seed_favorites(["101"])
        self.subscribe()
        self.engine.run(trigger="test")
        row = self.db.find_track("netease", "101")
        # 造一个同名不同路径的副本(模拟另一个专辑版本)
        copy_rel = "备份/周杰伦 - 晴天.wav"
        src = self.cfg.music_dir / row["path"]
        dst = self.cfg.music_dir / copy_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())

        stats = self.engine.run(trigger="test")     # 先把副本纳管
        self.assertGreaterEqual(stats.get("adopted", 0), 1, stats)

        src.unlink()
        stats = self.engine.run(trigger="test")
        self.assertEqual(stats["missing"], 1, stats)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM actions", default=0), 0,
                         "库里还有副本, 不该取消平台收藏")
        self.assertIn("101", self.ne.favorites())

    def test_search_single_download(self) -> None:
        """面板搜歌单曲下载: 不订阅歌单也能把一首歌下进库, 重复下载要跳过."""
        hits = self.ne.search("晴天", limit=5)
        self.assertTrue(hits, "mock 目录里应能搜到 晴天")
        track = hits[0]
        res = self.engine.download_single(track)
        self.assertTrue(res.get("ok"), res)
        row = self.db.find_track("netease", str(track.remote_id))
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "active", row)
        self.assertTrue((self.cfg.music_dir / row["path"]).exists())
        # 再下一遍: 库里已有 -> skipped, 不重复落文件
        res2 = self.engine.download_single(track)
        self.assertTrue(res2.get("ok") and res2.get("skipped"), res2)

    def test_dry_run_changes_nothing(self) -> None:
        self.db.kv_set("dry_run", "1")
        self.ne.seed_favorites(["101", "102"])
        self.subscribe()
        stats = self.engine.run(trigger="test")
        self.assertEqual(stats["downloaded"], 0)
        self.assertEqual(stats["download_skipped"], 2)
        self.assertEqual(self.audio_files(), [])

    def test_never_delete_policy(self) -> None:
        self.db.kv_set("delete_policy", "never")
        self.ne.seed_favorites(["101", "102"])
        self.subscribe()
        self.engine.run(trigger="test")
        self.assertEqual(len(self.audio_files()), 2)
        self.ne.remove_tracks("fav", ["101"])
        stats = self.engine.run(trigger="test")
        self.assertEqual(stats["deleted"], 0)
        self.assertEqual(len(self.audio_files()), 2, "策略 never 不允许删文件")

    def test_unmanaged_file_not_deleted(self) -> None:
        """用户原有文件(managed=0)即使歌单里没了也不能删."""
        self.ne.seed_favorites(["101"])
        self.subscribe()
        self.engine.run(trigger="test")
        row = self.db.find_track("netease", "101")
        self.db.execute("UPDATE tracks SET managed=0 WHERE id=?", (row["id"],))
        self.ne.remove_tracks("fav", ["101"])
        stats = self.engine.run(trigger="test")
        self.assertEqual(stats["deleted"], 0)
        self.assertEqual(len(self.audio_files()), 1)

    def test_shared_track_between_playlists(self) -> None:
        """同一首歌被两个歌单引用时, 从一个歌单移除不能删文件."""
        self.ne.seed_favorites(["101"])
        self.subscribe("fav")
        pid2 = self.db.upsert_playlist("netease", "pl2", "跑步歌单")
        self.ne.add_tracks("pl2", ["101"])
        self.engine.run(trigger="test")
        self.assertEqual(len(self.audio_files()), 1)
        self.ne.remove_tracks("fav", ["101"])
        stats = self.engine.run(trigger="test")
        self.assertEqual(stats["deleted"], 0, "另一个歌单还在引用")
        self.assertEqual(len(self.audio_files()), 1)
        del pid2

    def test_orphan_rows_cleaned_but_files_kept(self) -> None:
        """孤儿记录: 未下载的元数据清掉; 已下载的曲目行与文件都保留."""
        self.ne.seed_favorites(["101", "102"])
        pid = self.subscribe()
        self.engine.run(trigger="test")
        self.assertEqual(len(self.audio_files()), 2)

        # 造一条"不在任何歌单里"的待下载记录
        self.db.upsert_track({"provider": "netease", "remote_id": "999",
                              "title": "孤儿曲目", "artists": "X", "status": "pending"})
        self.assertEqual(self.db.cleanup_orphans(), 1)
        self.assertIsNone(self.db.find_track("netease", "999"))

        self.db.drop_playlist(pid)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM tracks"), 2,
                         "已下载的曲目行要保留(文件还在)")
        self.assertEqual(len(self.audio_files()), 2, "已下载文件不能被删")

    def test_empty_remote_response_does_not_wipe(self) -> None:
        """平台接口返回空但本地有记录时, 必须跳过而不是清空+删文件."""
        self.ne.seed_favorites(["101", "102"])
        pid = self.subscribe()
        self.engine.run(trigger="test")
        self.assertEqual(len(self.audio_files()), 2)

        orig = self.ne.playlist
        def fake(remote_id: str):
            pl = orig(remote_id)
            pl.tracks = []          # 模拟接口抽风
            pl.declared_count = 2   # 但平台说自己有 2 首
            return pl
        self.ne.playlist = fake  # type: ignore[assignment]
        stats = self.engine.run(trigger="test")
        self.assertEqual(stats["playlist_errors"], 1, stats)
        self.assertEqual(len(self.audio_files()), 2, "不能因为空响应删文件")
        self.assertEqual(self.db.playlist_track_ids(pid).__len__(), 2, "成员关系不能被清空")
        self.ne.playlist = orig  # type: ignore[assignment]

    def test_dedup_reuses_existing_file(self) -> None:
        """跨平台同曲: 已有同首歌时复用文件, 不重复下载."""
        self.ne.seed_favorites(["101"])
        pid1 = self.subscribe()
        self.engine.run(trigger="test")
        before = len(self.audio_files())

        # QQ 目录里也有同名同艺人的歌(mock 目录一致), 挂到 QQ 歌单并标待下载
        # 把 QQ 的音质上限调低(128k): 已有的文件明显更好, 才会走"复用"而不是重下
        self.cfg.qq.max_quality = "standard"
        qq = self.providers["qq"]
        qq.add_tracks("fav", ["101"])
        pid2 = self.db.upsert_playlist("qq", "fav", "我喜欢的音乐", "", 1)
        tid = self.db.upsert_track({"provider": "qq", "remote_id": "101", "title": "晴天",
                                    "artists": "周杰伦", "album": "叶惠美",
                                    "albumartist": "周杰伦", "duration": 10,
                                    "status": "pending"})
        self.db.set_playlist_items(pid2, [tid])
        stats = self.engine.run(trigger="test")
        self.assertEqual(len(self.audio_files()), before, "去重后不应新增文件")
        qq_row = self.db.find_track("qq", "101")
        self.assertEqual(qq_row["status"], "active")
        self.assertEqual(qq_row["path"],
                         self.db.find_track("netease", "101")["path"],
                         "复用应指向同一个文件")
        del pid1

    def test_dedup_keeps_unmanaged_protection(self) -> None:
        """复用用户原有文件时, 新记录必须继承 managed=0, 不能被当成可删文件."""
        self.ne.seed_favorites(["101"])
        self.subscribe()
        self.engine.run(trigger="test")
        # 把已下载文件标记为"用户原有文件"
        row = self.db.find_track("netease", "101")
        self.db.execute("UPDATE tracks SET managed=0 WHERE id=?", (row["id"],))

        # 另一个平台的同曲, 走去重
        qq = self.providers["qq"]
        qq.add_tracks("fav", ["101"])
        pid2 = self.db.upsert_playlist("qq", "fav", "我喜欢", "", 1)
        tid = self.db.upsert_track({"provider": "qq", "remote_id": "101", "title": "晴天",
                                    "artists": "周杰伦", "duration": 10, "status": "pending"})
        self.db.set_playlist_items(pid2, [tid])
        self.engine.run(trigger="test")
        dedup_row = self.db.find_track("qq", "101")
        self.assertEqual(dedup_row["path"], row["path"])
        self.assertEqual(int(dedup_row["managed"]), 0,
                         "复用用户原有文件时不能标成可删")

    def test_downloaded_files_get_shared_permissions(self) -> None:
        """归档时必须把权限放开到 a+rwX, 否则 SMB/Windows 用户打不开改不了.

        (mutagen 保存标签会重建文件, 新文件是 700 root:root —— 这是真踩过的坑)
        """
        from unittest import mock

        self.ne.seed_favorites(["101"])
        self.subscribe()
        target_dir = self.cfg.music_dir / "某艺人" / "某专辑"
        target_dir.mkdir(parents=True)
        target = target_dir / "某艺人 - 某歌.wav"
        target.write_bytes(b"x")

        with mock.patch("app.organizer.os.chmod") as patched:
            self.engine.organizer.fix_perms(target, self.cfg.music_dir)
        modes = [c.args[1] for c in patched.call_args_list]
        self.assertIn(0o666, modes, f"文件权限没放开: {modes}")
        self.assertIn(0o777, modes, f"目录权限没放开: {modes}")

    # ---------- 库内文件变动: 移动 / 新增 / 失败重试 ----------
    def test_moved_file_is_followed_not_deleted(self) -> None:
        """用户整理目录(改名/挪位置)不能被当成"删除", 更不能因此取消平台收藏."""
        self.ne.seed_favorites(["101"])
        self.subscribe()
        self.engine.run(trigger="test")
        old = self.audio_files()[0]

        new = self.cfg.music_dir / "整理后" / old.name
        new.parent.mkdir(parents=True, exist_ok=True)
        old.rename(new)

        stats = self.engine.run(trigger="test")
        self.assertEqual(stats["missing"], 0, stats)
        self.assertEqual(stats["moved"], 1, stats)
        row = self.db.find_track("netease", "101")
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["path"], "整理后/" + old.name)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM actions", default=0), 0,
                         "只是移动文件, 不该产生取消收藏动作")

    def test_no_unfavorite_when_other_copy_exists(self) -> None:
        """库里还有同一首歌的别的副本时, 删掉一个不该取消平台收藏."""
        self.ne.seed_favorites(["101"])
        self.subscribe()
        self.engine.run(trigger="test")
        row = self.db.find_track("netease", "101")
        src = self.cfg.music_dir / row["path"]

        dup_rel = "备份/晴天(副本).wav"
        dup = self.cfg.music_dir / dup_rel
        dup.parent.mkdir(parents=True, exist_ok=True)
        dup.write_bytes(src.read_bytes())
        self.db.upsert_track({
            "provider": "local", "remote_id": dup_rel, "title": row["title"],
            "artists": row["artists"], "album": row["album"],
            "albumartist": row["albumartist"], "duration": row["duration"],
            "path": dup_rel, "managed": 0, "status": "active",
        })

        src.unlink()
        stats = self.engine.run(trigger="test")
        self.assertGreaterEqual(stats["missing"], 1, stats)
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM actions", default=0), 0,
                         "还有副本存活时不能取消平台收藏")

    def test_external_file_is_adopted_as_unmanaged(self) -> None:
        """用户用别的工具往库里放歌, 应自动纳管且标记为"永不删除"."""
        ext_rel = "别人给的/某艺人 - 某歌.wav"
        ext = self.cfg.music_dir / ext_rel
        ext.parent.mkdir(parents=True, exist_ok=True)
        ext.write_bytes(self.ne._audio["101"])

        stats = self.engine.run(trigger="test")
        self.assertEqual(stats["adopted"], 1, stats)
        row = self.db.find_track("local", ext_rel)
        self.assertIsNotNone(row, "外部文件应被纳管")
        self.assertEqual(int(row["managed"]), 0, "纳管的文件必须标记为不可删除")

    def test_stale_failure_gets_retried(self) -> None:
        """失败很久的曲目应被重置 attempts 再试一次(音源可能后来上架)."""
        self.ne.seed_favorites(["101"])
        self.subscribe()
        self.engine.run(trigger="test")
        row = self.db.find_track("netease", "101")
        self.assertEqual(row["status"], "active")

        # 假装它当时下载失败、已重试 3 次、且文件没留下
        self.db.execute(
            "UPDATE tracks SET status='failed', attempts=3, path='', "
            "updated_at='2020-01-01T00:00:00+0800' WHERE id=?", (row["id"],),
        )
        for f in self.audio_files():
            f.unlink()

        stats = self.engine.run(trigger="test")
        self.assertGreaterEqual(stats["retry_reset"], 1, stats)
        self.assertEqual(self.db.find_track("netease", "101")["status"], "active",
                         "重置后应重新下载成功")

    def test_skip_download_when_better_copy_exists(self) -> None:
        """库里已有更高音质的同曲文件时, 不该再去下载低音质版本."""
        rel = "已有/周杰伦 - 晴天.wav"
        self.write_wav(rel, rate=44100, width=2)     # 16bit/44.1k, 分 1204100
        self.db.upsert_track({"provider": "local", "remote_id": rel, "title": "晴天",
                              "artists": "周杰伦", "album": "叶惠美", "albumartist": "周杰伦",
                              "duration": 10, "path": rel, "size": 2000, "ext": "wav",
                              "managed": 0, "status": "active"})

        self.ne.seed_favorites(["101"])
        self.subscribe()
        stats = self.engine.run(trigger="test")

        self.assertEqual(stats["downloaded"], 0, stats)
        self.assertEqual(stats["dedup_reused"], 1, stats)
        self.assertEqual(len(self.audio_files()), 1, "不该产生新文件")
        row = self.db.find_track("netease", "101")
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["path"], rel, "应直接复用已有高音质文件")
        self.assertEqual(int(row["managed"]), 0, "复用原库文件时不能变成可删")

    def test_auto_upgrade_replaces_lower_quality(self) -> None:
        """已有低音质时, 下载到更好的版本后自动把差版本移入回收目录."""
        bad_rel = "旧/艺人 - 歌.wav"
        bad = self.write_wav(bad_rel, rate=4000, width=1)        # 8bit/4k
        good_rel = "新/艺人 - 歌.wav"
        self.write_wav(good_rel, rate=44100, width=2)            # 16bit/44.1k

        self.db.upsert_track({"provider": "local", "remote_id": bad_rel, "title": "歌",
                              "artists": "艺人", "album": "旧专辑", "albumartist": "艺人",
                              "duration": 10, "path": bad_rel, "size": 500, "ext": "wav",
                              "managed": 0, "status": "active"})
        tid = self.db.upsert_track({"provider": "netease", "remote_id": "901",
                                    "title": "歌", "artists": "艺人", "album": "新专辑",
                                    "albumartist": "艺人", "duration": 10,
                                    "path": good_rel, "size": 9000, "ext": "wav",
                                    "managed": 1, "status": "active"})

        kept = self.engine._auto_upgrade(dict(self.db.get_track(tid)), good_rel)

        self.assertEqual(kept, "", "新文件音质更好, 应保留")
        self.assertFalse(bad.exists(), "旧的低音质文件应被移走")
        self.assertTrue((self.cfg.music_dir / ".musehub_trash").exists())
        old_row = self.db.find_track("local", bad_rel)
        self.assertEqual(old_row["path"], good_rel, "旧记录应改指新文件")
        self.assertEqual(old_row["status"], "active", "不能变成 deleted/missing")
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM actions", default=0), 0)

    def test_upgrade_keeps_best_quality_and_never_unfavorites(self) -> None:
        """洗版: 保留真实音质最好的文件; 记录改指保留文件; 绝不取消平台收藏."""
        import wave as _wave

        def make_wav(path: Path, rate: int, width: int) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with _wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(width)
                w.setframerate(rate)
                w.writeframes(b"\x00" * (rate // 4) * width)

        # 先正常下载一首(mock: 16bit 8kHz)
        self.ne.seed_favorites(["101"])
        self.subscribe()
        self.engine.run(trigger="test")
        row1 = self.db.find_track("netease", "101")
        good = self.cfg.music_dir / row1["path"]
        self.assertTrue(good.exists())

        # 造一个差音质副本(另一平台的同曲记录, 8bit 4kHz), 模拟历史重复下载
        bad_rel = "B/晴天(差版本).wav"
        make_wav(self.cfg.music_dir / bad_rel, 4000, 1)
        self.db.upsert_track({"provider": "qq", "remote_id": "dup101",
                              "title": row1["title"], "artists": row1["artists"],
                              "album": "差专辑", "duration": row1["duration"],
                              "path": bad_rel, "size": 800, "ext": "wav",
                              "managed": 1, "status": "active"})

        stats = self.engine.dedupe_apply(keep="best")
        self.assertEqual(stats["groups"], 1, stats)
        self.assertEqual(stats["removed_files"], 1, stats)

        r2 = self.db.find_track("qq", "dup101")
        self.assertEqual(r2["path"], row1["path"], "差版本的记录应改指最佳文件")
        self.assertEqual(r2["status"], "active", "改指后必须仍是 active")
        self.assertFalse((self.cfg.music_dir / bad_rel).exists())
        trash = self.cfg.music_dir / ".musehub_trash"
        self.assertTrue(trash.exists() and any(trash.rglob("*")), "差文件应在回收目录")
        # 核心安全断言: 洗版绝不能产生取消收藏动作
        self.assertEqual(self.db.scalar("SELECT COUNT(*) FROM actions", default=0), 0)

        # 再跑一轮完整同步: 好文件不能被连带删, 不能取消收藏
        stats2 = self.engine.run(trigger="test")
        self.assertTrue(good.exists(), "共享的好文件不能被连带删除")
        self.assertEqual(stats2["actions_done"], 0, stats2)
        self.assertEqual(stats2["downloaded"], 0, stats2)
        self.assertEqual(stats2["missing"], 0, stats2)
        self.assertEqual(self.db.find_track("netease", "101")["status"], "active")

        # 洗版后同曲只剩一个文件 -> 不该再出现在重复列表里
        self.assertEqual(self.engine.find_duplicates(), [],
                         "洗版完成后该组应从重复列表消失")

    def test_trash_stats_and_empty(self) -> None:
        """回收目录能统计占用并一键清空."""
        trash = self.cfg.music_dir / ".musehub_trash" / "20261024"
        trash.mkdir(parents=True)
        (trash / "a.flac").write_bytes(b"x" * 1000)
        (trash / "b.flac").write_bytes(b"x" * 500)

        st = self.engine.trash_stats()
        self.assertEqual(st["files"], 2)
        self.assertEqual(st["bytes"], 1500)
        self.assertEqual(len(st["by_day"]), 1)

        r = self.engine.trash_empty()
        self.assertEqual(r["files"], 2)
        self.assertEqual(r["bytes"], 1500)
        self.assertFalse((self.cfg.music_dir / ".musehub_trash").exists())
        self.assertEqual(self.engine.trash_stats()["files"], 0)

    def test_fallback_to_other_platform_when_no_source(self) -> None:
        """本平台无音源时, 自动到另一个平台搜同曲下载."""
        from app.providers.base import NoSource
        self.ne.seed_favorites(["101"])
        self.subscribe()

        def no_source(t, q):
            raise NoSource("网易云无可用音源")
        self.ne.source = no_source  # type: ignore[assignment]

        stats = self.engine.run(trigger="test")
        self.assertEqual(stats["downloaded"], 1, stats)
        row = self.db.find_track("netease", "101")
        self.assertEqual(row["status"], "active", "应通过 QQ 回退下载成功")

    def test_lyric_and_cover_sidecars(self) -> None:
        self.ne.seed_favorites(["101"])
        self.subscribe()
        self.engine.run(trigger="test")
        # mock 没有歌词, 但 cover 目录里不应出现半成品文件
        self.assertTrue(self.audio_files())
        for f in self.audio_files():
            self.assertGreater(f.stat().st_size, 64 * 1024)


class TestOrganizer(unittest.TestCase):
    def test_sanitize(self) -> None:
        self.assertEqual(sanitize_component('a/b:c*?"<>|'), "a_b_c______")
        self.assertEqual(sanitize_component("  .hidden.  "), "hidden")
        self.assertEqual(sanitize_component("CON"), "_CON")
        self.assertEqual(sanitize_component(""), "_")

    def test_long_component_truncated(self) -> None:
        out = sanitize_component("中" * 200, max_bytes=60)
        self.assertLessEqual(len(out.encode("utf-8")), 60)


if __name__ == "__main__":
    unittest.main(verbosity=2)
