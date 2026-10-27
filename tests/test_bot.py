"""TG 机器人逻辑测试(离线, 假 API)."""
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
from app.organizer import Organizer
from app.providers.registry import build_mock_providers
from app.sync import SyncEngine
from app.tg.api import TgError, chunks, esc, keyboard, strip_html
from app.tg.bot import Bot


class FakeApi:
    def __init__(self):
        self.fail_html = False          # 模拟 Telegram 的 "can't parse entities"
        self.sent: list[tuple[int, str]] = []
        self.markups: list[dict | None] = []
        self.answered: list[str] = []

    def send(self, chat_id, text, markup=None, reply_to=None, html=True):
        if self.fail_html and html:
            raise TgError("sendMessage 失败: Bad Request: can't parse entities "
                          "(400)")
        self.sent.append((chat_id, text))
        self.markups.append(markup)
        return {"message_id": len(self.sent)}

    def edit(self, chat_id, message_id, text, markup=None):
        return {}

    def answer_callback(self, cb_id, text="", alert=False):
        self.answered.append(cb_id)

    def get_updates(self, offset, timeout=25):
        return []

    def me(self):
        return {"username": "musehub_bot"}


def make_bot(admin_id: int = 42):
    tmp = Path(tempfile.mkdtemp(prefix="musehub-bot-"))
    os.environ["DATA_DIR"] = str(tmp / "data")
    os.environ["MUSIC_DIR"] = str(tmp / "music")
    os.environ["MUSEHUB_MOCK"] = "1"
    os.environ["DRY_RUN"] = "0"
    cfg = Config.load(env_file=tmp / "none.env")
    cfg.telegram.token = "fake"
    cfg.telegram.admin_ids = (admin_id,)
    db = Database(cfg.db_path)
    db.kv_set("dry_run", "0")
    ctx = type("Ctx", (), {})()
    ctx.cfg = cfg
    ctx.db = db
    ctx.providers = build_mock_providers(cfg, db)
    ctx.navidrome = Navidrome(cfg.navidrome, db)
    ctx.engine = SyncEngine(cfg, db, ctx.providers,
                            Downloader(cfg, db, ctx.providers),
                            Organizer(cfg.organizer), ctx.navidrome)
    ctx.status = lambda: {
        "dry_run": ctx.engine.dry_run, "delete_policy": ctx.engine.delete_policy,
        "syncing": False, "stats": db.stats(), "providers": {k: p.status() for k, p in ctx.providers.items()},
    }
    ctx.trigger_sync = lambda trigger="manual", fetch_remote=True: True
    bot = Bot(ctx)
    bot.api = FakeApi()
    return bot, ctx


class TestBot(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        for p in getattr(cls, "_providers", []):
            p.stop()

    def test_help_and_status(self):
        bot, ctx = make_bot()
        type(self). _providers = list(ctx.providers.values())
        bot._cmd_help(1, "")
        bot._cmd_status(1, "")
        texts = [t for _, t in bot.api.sent]
        self.assertIn("MuseHub 指令", texts[0])
        self.assertIn("曲目", texts[1])

    def test_playlists_empty(self):
        bot, ctx = make_bot()
        type(self)._providers = list(ctx.providers.values())
        bot._cmd_playlists(1, "")
        self.assertIn("还没有订阅", bot.api.sent[0][1])

    def test_unauthorized(self):
        bot, ctx = make_bot()
        type(self)._providers = list(ctx.providers.values())
        bot._dispatch({"message": {"chat": {"id": 1}, "from": {"id": 999},
                                     "text": "/status"}})
        self.assertIn("无权限", bot.api.sent[0][1])

    def test_search_returns_buttons(self):
        bot, ctx = make_bot()
        type(self)._providers = list(ctx.providers.values())
        bot._cmd_search(1, "晴天")
        import time
        deadline = time.time() + 10
        while time.time() < deadline and not bot.api.sent:
            time.sleep(0.2)
        self.assertTrue(bot.api.sent)
        text = bot.api.sent[0][1]
        self.assertIn("晴天", text)

    def test_menu_has_inline_buttons(self):
        """/menu 必须给出可点的按钮(手机端不记命令)."""
        bot, ctx = make_bot()
        bot._dispatch({"message": {"chat": {"id": 1}, "from": {"id": 42}, "text": "/menu"}})
        markup = bot.api.markups[-1]
        self.assertIsNotNone(markup, "菜单应带 inline 键盘")
        datas = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
        self.assertIn("menu|status", datas)
        self.assertIn("menu|missing", datas)
        self.assertIn("menu|sync", datas)

    def test_missing_command_reports_backlog(self):
        """查库补全: 要报出还没下到的曲目数."""
        import time as _t
        bot, ctx = make_bot()
        pid = ctx.db.upsert_playlist("netease", "fav", "我喜欢")
        ctx.db.execute("UPDATE playlists SET subscribed=1 WHERE id=?", (pid,))
        tid = ctx.db.upsert_track({"provider": "netease", "remote_id": "999",
                                   "title": "没下到", "artists": "某人", "status": "failed"})
        ctx.db.set_playlist_items(pid, [tid])

        bot._cmd_missing(1, "")
        for _ in range(20):
            if bot.api.sent:
                break
            _t.sleep(0.05)
        text = bot.api.sent[-1][1]
        self.assertIn("待补下载", text)
        self.assertIn("失败: 1", text)
        datas = [b["callback_data"]
                 for row in bot.api.markups[-1]["inline_keyboard"] for b in row]
        self.assertIn("miss|retry", datas)

    def test_menu_callback_dispatches(self):
        """点菜单按钮应真的执行对应命令."""
        bot, ctx = make_bot()
        bot._on_callback({"id": "cb1", "from": {"id": 42}, "data": "menu|status",
                          "message": {"chat": {"id": 1}, "message_id": 5}})
        self.assertTrue(bot.api.sent, "应回复状态")
        self.assertIn("MuseHub", bot.api.sent[-1][1])

    def test_playlists_output_is_html_safe(self):
        """歌单列表里的 <序号> 之类必须转义, 否则 Telegram 400, 用户以为按钮坏了."""
        import re as _re
        bot, ctx = make_bot()
        ctx.db.upsert_playlist("netease", "fav", "我喜欢的音乐")
        bot._cmd_playlists(1, "")
        text = bot.api.sent[-1][1]
        bad = _re.findall(r"<(?![/]?(?:b|strong|i|em|u|s|code|pre|a)\b)[^>]{1,30}>", text)
        self.assertEqual(bad, [], f"存在未转义的尖括号: {bad}")

    def test_send_falls_back_to_plain_on_parse_error(self):
        """HTML 解析失败时必须降级成纯文本重发 —— 不能让用户看到"没反应"."""
        bot, ctx = make_bot()
        bot.api.fail_html = True
        bot._cmd_playlists(1, "")
        self.assertTrue(bot.api.sent, "降级后应至少发出一条纯文本消息")
        self.assertIn("歌单", bot.api.sent[-1][1])

    def test_strip_html(self):
        self.assertEqual(strip_html("<b>粗</b> &lt;序号&gt;"), "粗 <序号>")

    def test_unknown_command(self):
        bot, ctx = make_bot()
        type(self)._providers = list(ctx.providers.values())
        bot._dispatch({"message": {"chat": {"id": 1}, "from": {"id": 42},
                                     "text": "/nope"}})
        self.assertIn("未知指令", bot.api.sent[0][1])

    def test_utils(self):
        self.assertEqual(esc("<b>&"), "&lt;b&gt;&amp;")
        self.assertEqual(chunks("a" * 5000, 3800).__len__(), 2)
        kb = keyboard([[(f"⬇️ {i}", f"dl|{i}") for i in range(1, 4)]])
        self.assertEqual(len(kb["inline_keyboard"][0]), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
