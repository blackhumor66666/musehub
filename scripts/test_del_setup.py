"""删除链路实测: 阶段1 - 准备(把测试曲加进网易云『我喜欢』, 防误下载)."""
import sys

from app.config import Config
from app.db import Database
from app import settings
from app.providers.registry import make_providers

cfg = Config.load()
db = Database(cfg.db_path)
settings.load_into(cfg, db)
ps = make_providers(cfg, db)
p = ps["netease"]

FAV = "2050453492"   # 账号真正的『我喜欢』(specialType=5 自动探测到的)

# 1) 暂停「盘点」歌单, 让测试曲只被『我喜欢』引用
db.execute("update playlists set subscribed=0 where id=3")

# 2) 选一首已下载的网易云歌
a = db.query(
    "select remote_id, title, artists, path from tracks "
    "where provider='netease' and status='active' and path<>'' limit 1"
)[0]
print(f"测试曲目: {a['remote_id']} {a['title']} - {a['artists']}")
print("文件存在:", __import__("os").path.exists("/music/" + a["path"]))

# 3) 预拉『我喜欢』曲目并标记 attempts=5, 防止同步时顺带下载
pl = p.playlist(FAV)
print("『我喜欢』曲目数:", len(pl.tracks))
row_id = db.upsert_playlist("netease", FAV, pl.name or "我喜欢", pl.cover, len(pl.tracks))
db.execute("update playlists set subscribed=1 where id=?", (row_id,))
for t in pl.tracks:
    db.upsert_track({**t.to_row(), "status": "pending"})
db.execute(
    """update tracks set attempts=5
       where provider='netease' and status='pending'
         and id in (select track_id from playlist_items where playlist_id=?)""",
    (row_id,),
)
print("已防止误下载(待下载 attempts=5):",
      db.scalar("select count(*) from tracks where attempts=5", default=0))

# 4) 把测试曲加入『我喜欢』(收藏)
p.add_tracks(FAV, [a["remote_id"]])
print("已加入『我喜欢』: TEST_REMOTE_ID=" + a["remote_id"])
print("TEST_PATH=" + a["path"])
