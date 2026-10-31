"""自检: 把"数据层面对不上"的地方查出来, 不用等人肉发现.

用法: python scripts/selfcheck.py [--verbose]
会检查:
  1. 最近同步是否报错
  2. 数据库说 active 但文件不在了
  3. 文件在但数据库没有记录(孤儿文件)
  4. 失败的回推动作(按平台/原因)
  5. 歌单引用缺失 / 播放器镜像状态
  6. 长时间卡在 pending 的曲目
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Config          # noqa: E402
from app.db import Database            # noqa: E402


def main() -> int:
    cfg = Config.load()
    db = Database(cfg.db_path)
    problems: list[str] = []

    print("=" * 64)
    print("MuseHub 自检")
    print("=" * 64)

    # 1) 最近同步错误
    errs = db.query(
        "SELECT ts, message FROM events WHERE level='error' ORDER BY id DESC LIMIT 5")
    if errs:
        print(f"\n[1] 最近的错误({len(errs)} 条)")
        for e in errs[:5]:
            print(f"    {e['ts'][5:19]}  {e['message'][:90]}")
        problems.append(f"{len(errs)} 条历史错误(见上)")
    else:
        print("\n[1] 同步日志: 无错误 ✓")

    # 2) active 但文件不存在
    rows = [dict(r) for r in db.query(
        "SELECT id, title, artists, path FROM tracks WHERE status='active' AND path<>''")]
    missing = [r for r in rows if not (cfg.music_dir / r["path"]).exists()]
    if missing:
        print(f"\n[2] 数据库 active 但文件不存在: {len(missing)} 条")
        for r in missing[:8]:
            print(f"    #{r['id']} {r['artists']} - {r['title']}  ({r['path']})")
        problems.append(f"{len(missing)} 条记录指向不存在的文件")
    else:
        print(f"\n[2] 文件一致性: {len(rows)} 条 active 记录全部有文件 ✓")

    # 3) 孤儿文件(库里有文件但没记录)
    known = {r["path"] for r in rows}
    exts = {".flac", ".mp3", ".m4a", ".wav", ".ogg", ".ape", ".wma", ".aac"}
    orphans = []
    trash_dir = ".musehub_trash"
    for f in cfg.music_dir.rglob("*"):
        if trash_dir in f.parts:      # 回收站里的不算孤儿
            continue
        if f.suffix.lower() in exts:
            rel = f.relative_to(cfg.music_dir).as_posix()
            if rel not in known:
                orphans.append(rel)
    if orphans:
        print(f"\n[3] 库里有文件但数据库没记录: {len(orphans)} 个（可在「库体检」里导入）")
        for o in orphans[:5]:
            print(f"    {o}")
        problems.append(f"{len(orphans)} 个孤儿文件未入库")
    else:
        print("[3] 孤儿文件: 无 ✓")

    # 4) 失败的回推动作
    acts = [dict(r) for r in db.query(
        "SELECT provider, state, last_error FROM actions WHERE state IN ('failed','queued')")]
    if acts:
        c = Counter((a["provider"], a["state"]) for a in acts)
        print(f"\n[4] 待处理/失败的回推动作: {len(acts)} 条")
        for (prov, st), n in c.items():
            print(f"    {prov:8s} {st:8s} {n}")
        errs2 = Counter((a["last_error"] or "")[:40] for a in acts if a["last_error"])
        for msg, n in errs2.most_common(3):
            print(f"      原因 x{n}: {msg}")
        if any(a["provider"] not in ("netease", "qq") for a in acts):
            problems.append("存在非平台(如 local)的回推动作, 应该被过滤")
    else:
        print("\n[4] 回推队列: 空 ✓")

    # 5) 歌单与镜像
    pls = [dict(r) for r in db.query("SELECT * FROM playlists WHERE subscribed=1")]
    print(f"\n[5] 订阅歌单 {len(pls)} 个")
    for p in pls:
        items = db.scalar("SELECT COUNT(*) FROM playlist_items WHERE playlist_id=?", (p["id"],), 0)
        alive = db.scalar(
            """SELECT COUNT(*) FROM playlist_items pi JOIN tracks t ON t.id=pi.track_id
               WHERE pi.playlist_id=? AND t.status='active'""", (p["id"],), 0)
        mirrors = db.query("SELECT account, navidrome_id FROM playlist_mirrors WHERE playlist_id=?",
                           (p["id"],))
        flag = ""
        if p["subscribed"] and not p.get("navidrome_name"):
            flag = "  ⚠ 无播放器镜像"
        print(f"    #{p['id']} {p['name'][:24]:26s} 曲目 {items:4d} 已入库 {alive:4d} "
              f"镜像 {len(mirrors)}{flag}")

    # 6) 卡住的 pending
    stuck = db.scalar(
        "SELECT COUNT(*) FROM tracks WHERE status='pending' AND attempts>=3", (), 0)
    if stuck:
        print(f"\n[6] 重试 >=3 次仍失败的下载: {stuck} 条（下载任务页可重试/移除）")
        problems.append(f"{stuck} 条下载反复失败")

    by_status = {r["status"]: r["n"] for r in db.query(
        "SELECT status, COUNT(*) n FROM tracks GROUP BY status")}
    print(f"\n[7] 曲目状态分布: {by_status}")

    blocked = db.scalar("SELECT COUNT(*) FROM blocked_tracks", (), 0)
    ignored = db.scalar("SELECT COUNT(*) FROM ignored", (), 0)
    print(f"    删除黑名单 {blocked} 条 · 体检忽略 {ignored} 条")

    print("\n" + "=" * 64)
    if problems:
        print("需要关注:")
        for p in problems:
            print("  -", p)
    else:
        print("未发现数据层面的问题 ✓")
    print("=" * 64)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
