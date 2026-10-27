"""通知模块测试: 开关、聚合、免打扰、Webhook 载荷."""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import NotifyConfig
from app.notify import Notifier, in_quiet, parse_quiet


class FakeDB:
    def __init__(self) -> None:
        self.logs: list[tuple] = []

    def log(self, level: str, source: str, message: str, **meta) -> None:
        self.logs.append((level, source, message, meta))


def make(**over):
    conf = NotifyConfig(**over)
    cfg = SimpleNamespace(notify=conf)
    sent: list[str] = []
    n = Notifier(cfg, FakeDB(), telegram_send=sent.append)
    return n, sent, conf


def make_with_photo(**over):
    """带图片通道的构造: photos 记录 (photo_url, caption)."""
    conf = NotifyConfig(**over)
    cfg = SimpleNamespace(notify=conf)
    sent: list[str] = []
    photos: list[tuple[str, str]] = []
    n = Notifier(cfg, FakeDB(), telegram_send=sent.append,
                 telegram_photo=lambda url, cap: photos.append((url, cap)) or True)
    return n, sent, photos, conf


class TestTrackCard(unittest.TestCase):
    def test_few_tracks_sent_as_photo_cards(self) -> None:
        """≤3 首时逐首发封面卡片, caption 带歌名/歌手/专辑/音质."""
        n, sent, photos, _ = make_with_photo()
        n.track_added("netease", "周杰伦", "晴天", "lossless", "周杰伦/叶惠美/x.flac",
                      info={"cover": "http://example.com/c.jpg", "album": "叶惠美",
                            "year": "2003", "duration": 269,
                            "size": 28_600_000, "ext": "flac"})
        n.flush_events()
        self.assertEqual(len(photos), 1, photos)
        url, cap = photos[0]
        self.assertEqual(url, "http://example.com/c.jpg")
        self.assertIn("<b>晴天</b>", cap)
        self.assertIn("周杰伦", cap)
        self.assertIn("叶惠美", cap)
        self.assertIn("2003", cap)
        self.assertIn("MB", cap)
        self.assertIn("4:29", cap)

    def test_no_cover_falls_back_to_text(self) -> None:
        n, sent, photos, _ = make_with_photo()
        n.track_added("netease", "A", "B", "lossless", "p/a.flac")
        n.flush_events()
        self.assertEqual(len(photos), 0)
        self.assertEqual(len(sent), 1)
        self.assertIn("<b>B</b>", sent[0])

    def test_many_tracks_aggregated(self) -> None:
        n, sent, photos, _ = make_with_photo()
        for i in range(6):
            n.track_added("qq", "艺人", f"歌{i}", "320k", f"p/歌{i}.mp3",
                          info={"cover": f"http://x/{i}.jpg"})
        n.flush_events()
        self.assertEqual(len(photos), 0, "超过 3 首应聚合, 不逐首发图")
        joined = "\n".join(sent)
        self.assertIn("（6）", joined)
        self.assertIn("歌5", joined)

    def test_photo_failure_falls_back_to_text(self) -> None:
        conf = NotifyConfig()
        cfg = SimpleNamespace(notify=conf)
        sent: list[str] = []
        n = Notifier(cfg, FakeDB(), telegram_send=sent.append,
                     telegram_photo=lambda url, cap: False)
        n.track_added("netease", "A", "B", "lossless", "p/a.flac",
                      info={"cover": "http://x/c.jpg"})
        n.flush_events()
        self.assertEqual(len(sent), 1, "图片发送失败要退回纯文本")


class TestQuiet(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertEqual(parse_quiet("23:30-08:00"), (1410, 480))
        self.assertEqual(parse_quiet("08:00-12:00"), (480, 720))
        self.assertIsNone(parse_quiet(""))
        self.assertIsNone(parse_quiet("abc"))
        self.assertIsNone(parse_quiet("25:00-08:00"))

    def test_in_quiet_overnight(self) -> None:
        # 2026-10-24 23:45 本地时间
        ts = time.mktime((2026, 10, 24, 23, 45, 0, 0, 0, -1))
        self.assertTrue(in_quiet("23:30-08:00", ts))
        noon = time.mktime((2026, 10, 24, 12, 0, 0, 0, 0, -1))
        self.assertFalse(in_quiet("23:30-08:00", noon))

    def test_in_quiet_same_day(self) -> None:
        ts = time.mktime((2026, 10, 24, 9, 0, 0, 0, 0, -1))
        self.assertTrue(in_quiet("08:00-12:00", ts))
        self.assertFalse(in_quiet("08:00-12:00", ts + 5 * 3600))


class TestSwitch(unittest.TestCase):
    def test_master_off_sends_nothing(self) -> None:
        n, sent, _ = make(enabled=False)
        n.track_added("netease", "周杰伦", "稻香", "lossless")
        n.finish({"downloaded": 1})
        self.assertEqual(sent, [])

    def test_kind_off(self) -> None:
        n, sent, _ = make(track_added=False)
        n.track_added("netease", "周杰伦", "稻香", "lossless")
        n.finish({})
        self.assertEqual(sent, [])

    def test_summary_off_but_added_on(self) -> None:
        n, sent, _ = make(summary=False)
        n.track_added("netease", "A", "B", "lossless")
        n.finish({"downloaded": 1})
        self.assertEqual(len(sent), 1)
        self.assertIn("新歌入库", sent[0])

    def test_empty_run_silent(self) -> None:
        n, sent, _ = make()
        n.finish({"downloaded": 0, "download_failed": 0, "deleted": 0})
        self.assertEqual(sent, [])


class TestBatching(unittest.TestCase):
    def test_many_tracks_one_message(self) -> None:
        n, sent, _ = make()
        for i in range(30):
            n.track_added("qq", "艺人", f"歌{i}", "320k")
        n.finish({"downloaded": 30})
        added = [m for m in sent if "新歌入库" in m]
        self.assertEqual(len(added), 1)
        self.assertIn("（30）", added[0])
        self.assertIn("还有 15 条", added[0])      # max_lines=15
        self.assertIn("同步完成", sent[-1])

    def test_verbose_off_only_count(self) -> None:
        n, sent, _ = make(verbose_tracks=False)
        n.track_added("qq", "艺人", "歌", "320k")
        n.finish({})
        self.assertEqual(sent[0], "🎵 <b>新歌入库 1 首</b>")

    def test_removed_and_failed_separate(self) -> None:
        n, sent, _ = make()
        n.track_removed("本地文件已删除，已排队取消平台收藏", "A", "B")
        n.track_failed("netease", "C", "D", "无音源")
        n.finish({})
        joined = "\n".join(sent)
        self.assertIn("歌曲删除", joined)
        self.assertIn("下载失败", joined)
        self.assertIn("无音源", joined)

    def test_playlist_change(self) -> None:
        n, sent, _ = make()
        n.playlist_change("网易云 我喜欢的音乐", 3, 1)
        n.playlist_change("网易云 空变化", 0, 0)
        n.finish({})
        self.assertEqual(len(sent), 1)
        self.assertIn("➕ 3 / ➖ 1", sent[0])
        self.assertNotIn("空变化", sent[0])


class TestQuietQueue(unittest.TestCase):
    def test_buffered_then_flushed(self) -> None:
        n, sent, conf = make()
        conf.quiet_hours = "23:30-08:00"
        # 直接塞进待发队列, 模拟免打扰期间
        n._pending.append("免打扰期间的一条")
        n._flush_pending()
        self.assertEqual(len(sent), 1)
        self.assertIn("积压 1 条", sent[0])
        self.assertIn("免打扰期间的一条", sent[0])

    def test_escape_html(self) -> None:
        n, sent, _ = make()
        n.track_added("qq", "<b>A</b>", "B & C", "320k")
        n.finish({})
        self.assertIn("&lt;b&gt;A&lt;/b&gt;", sent[0])
        self.assertIn("B &amp; C", sent[0])


class TestWebhook(unittest.TestCase):
    def test_payload(self) -> None:
        n, sent, conf = make(webhook_url="http://127.0.0.1:9/hook")
        captured: list[dict] = []

        class R:
            status_code = 200

        import app.notify as mod
        old = mod.requests
        mod.requests = SimpleNamespace(
            post=lambda url, json=None, timeout=None: (captured.append(json), R())[1])
        try:
            n.track_added("netease", "A", "B", "lossless")
            n.finish({})
        finally:
            mod.requests = old
        self.assertTrue(captured)
        body = captured[0]
        self.assertEqual(body["source"], "musehub")
        self.assertIn("event", body)
        self.assertIn("ts", body)
        self.assertTrue(sent)          # Telegram 仍然照发


if __name__ == "__main__":
    unittest.main()
