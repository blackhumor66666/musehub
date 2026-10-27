import sys
from app.config import Config
from app.db import Database
from app import settings as S
from app.notify import Notifier
from app.tg.api import TgApi

print("分组:", [g[0] for g in S.GROUPS])

cfg = Config.load()
db = Database(cfg.db_path)
S.load_into(cfg, db)
print("通知配置:", {
    "enabled": cfg.notify.enabled,
    "telegram": cfg.notify.telegram,
    "webhook": bool(cfg.notify.webhook_url),
    "quiet": cfg.notify.quiet_hours or "(无)",
    "max_lines": cfg.notify.max_lines,
})
print("admin_ids:", cfg.telegram.admin_ids)

api = TgApi(cfg.telegram.token, cfg.telegram.proxy)


def send(text):
    api.send(cfg.telegram.admin_ids[0], text)


n = Notifier(cfg, db, telegram_send=send)
ok, msg = n.test()
print("测试发送:", ok, msg)

# 再模拟一轮真实事件, 验证聚合效果
n.begin()
n.playlist_change("网易云 我喜欢的音乐", 3, 1)
n.track_added("netease", "周杰伦", "晴天", "无损 SQ (FLAC)")
n.track_added("qq", "陈奕迅", "孤勇者", "HQ 高品质 (320kbps MP3)")
n.track_removed("本地文件已删除，已排队取消平台收藏", "Uu", "那女孩对我说")
n.track_failed("netease", "某歌手", "某首歌", "本平台无音源")
n.finish({"downloaded": 2, "download_failed": 1, "deleted": 0,
          "actions_done": 1, "elapsed": 12.3})
print("模拟事件已发送")
db.close()
