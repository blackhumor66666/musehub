"""同步引擎.

一个周期做五件事, 顺序不能乱:

  1. reconcile_local  扫描音乐库, 发现"文件被删了" -> 记录 missing, 回推取消收藏
  2. pull_playlists   拉远端歌单, 与本地比对 -> 新增的排进下载队列, 移除的按策略删除
  3. download_pending 执行下载 + 打标签 + 归档
  4. push_actions     把需要回推平台的动作(取消收藏)发出去
  5. mirror_player    把本地状态镜像成 Navidrome 歌单

安全边界(很重要):
  * 只删除 managed=1 的文件, 也就是本程序自己下载的; 用户原有文件永不删
  * 同一文件被多个平台/歌单引用时, 只要还有引用就不删
  * delete_policy=never 时只报告不删除
  * dry_run=True 时全流程只记录不落地
"""
from __future__ import annotations

import os
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .db import Database, now_iso
from .downloader import DownloadError, Downloader
from .navidrome import Navidrome
from .notify import NULL as NULL_NOTIFIER
from .organizer import AUDIO_EXT, Organizer, TrackMeta
from .providers.base import BaseProvider, RemoteTrack, Unsupported

SKIP_DIRS = {"#recycle", "@eadir", ".@__thumb", "$recycle.bin", "system volume information"}


class SyncEngine:
    def __init__(self, cfg, db: Database, providers: dict[str, BaseProvider],
                 downloader: Downloader, organizer: Organizer, navidrome: Navidrome,
                 progress=None, notify=None):
        self.cfg = cfg
        self.db = db
        self.providers = providers
        self.downloader = downloader
        self.organizer = organizer
        self.navidrome = navidrome
        self.notify = notify or NULL_NOTIFIER
        if progress is None:
            from .progress import ProgressTracker
            progress = ProgressTracker()
        self.progress = progress
        self._running = False
        self._missing_ids: set[int] = set()
        # 同曲索引(标题+主艺人 -> 库内路径), 用于判断"还有没有别的副本"
        self._lib_index: dict[tuple[str, str], list[str]] = {}
        self._force_mirror = False   # 下一轮强制镜像播放器(手动触发时用)

    def provider_for(self, provider_name: str, account: str = "") -> BaseProvider | None:
        """按 (平台, 账号) 找会话; 账号缺省回落到平台主实例."""
        if account:
            p = self.providers.get(f"{provider_name}:{account}")
            if p is not None:
                return p
        return self.providers.get(provider_name)

    # ---------- 运行开关 ----------
    @property
    def dry_run(self) -> bool:
        override = self.db.kv_get("dry_run", "")
        if override in ("0", "1"):
            return override == "1"
        return self.cfg.sync.dry_run

    @property
    def delete_policy(self) -> str:
        return self.db.kv_get("delete_policy", "") or self.cfg.sync.delete_policy

    # ---------- 主流程 ----------
    def run(self, trigger: str = "manual", fetch_remote: bool = True) -> dict:
        if self._running:
            self.db.log("warn", "sync", "已有同步在进行, 本次跳过")
            return {"skipped": True}
        self._running = True
        self.notify.begin()
        run_id = self.db.start_run(trigger)
        stats: dict[str, Any] = {
            "trigger": trigger, "dry_run": self.dry_run, "delete_policy": self.delete_policy,
        }
        started = time.time()
        try:
            stats.update(self.reconcile_local())
            if fetch_remote:
                stats.update(self.pull_playlists())
            stats.update(self.download_pending())
            stats.update(self.push_actions())
            # 只在"这轮真的动了库"时才去写播放器 —— 空闲轮次不做无谓的全库索引
            touched = any(stats.get(k) for k in
                          ("moved", "missing", "restored", "deleted", "adopted",
                           "downloaded", "actions_done"))
            if fetch_remote or touched or self._force_mirror:
                stats.update(self.mirror_player())
            self._force_mirror = False
            stats["elapsed"] = round(time.time() - started, 1)
            self.db.finish_run(run_id, stats)
            self.db.log("info", "sync", self._summary(stats), **stats)
            self.notify.finish(stats)
            return stats
        except Exception as exc:  # noqa: BLE001 - 单次失败不能让调度器挂掉
            stats["error"] = str(exc)
            stats["elapsed"] = round(time.time() - started, 1)
            self.db.finish_run(run_id, stats, state="failed")
            self.db.log("error", "sync", f"同步失败: {exc}")
            self.notify.finish(stats)
            return stats
        finally:
            self._running = False

    @staticmethod
    def _summary(s: dict) -> str:
        return (
            f"同步完成: 新下载 {s.get('downloaded', 0)} / 失败 {s.get('download_failed', 0)} / "
            f"本地缺失 {s.get('missing', 0)} / 清理 {s.get('deleted', 0)} / "
            f"待回推 {s.get('actions_done', 0)} / 歌单 {s.get('playlists', 0)}"
            + (" (试运行)" if s.get("dry_run") else "")
        )

    # ---------- 1. 本地核对 ----------
    def scan_library(self) -> set[str]:
        root = self.cfg.music_dir
        found: set[str] = set()
        if not root.exists():
            return found
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                if Path(name).suffix.lower() in AUDIO_EXT:
                    full = Path(dirpath) / name
                    try:
                        found.add(full.relative_to(root).as_posix())
                    except ValueError:
                        continue
        return found

    def reconcile_local(self) -> dict:
        self._missing_ids = set()
        found = self.scan_library()
        known = self.db.active_paths()
        unmanaged = found - known
        stats = {"library_files": len(found), "missing": 0, "restored": 0, "deleted": 0,
                 "unmanaged_files": len(unmanaged), "moved": 0, "adopted": 0,
                 "retry_reset": 0}

        # ---------- 1) 文件不见了: 先判断是"移动/改名"还是真删 ----------
        # 关键: 用户用 Music Tag Web 或手动整理目录后, 旧路径会失效。
        # 若直接当成"删除"去平台取消收藏, 会误删用户真实收藏 —— 必须区分开。
        for r in self.db.query(
            "SELECT title, artists, path FROM tracks WHERE status='active' AND path<>''"
        ):
            key = (self._norm(r["title"]), self._norm((r["artists"] or "").split(";")[0]))
            self._lib_index.setdefault(key, []).append(r["path"])

        by_base: dict[str, list[str]] = {}
        for p in unmanaged:
            by_base.setdefault(p.rsplit("/", 1)[-1].casefold(), []).append(p)

        seen: set[str] = set()
        for row in [dict(r) for r in self.db.tracks_by_status("active")]:
            path = row["path"]
            if not path or path in found or path in seen:
                continue
            seen.add(path)
            cands = by_base.get(path.rsplit("/", 1)[-1].casefold()) or []
            if len(cands) == 1:
                # 同名文件只在一个新位置出现 -> 判定为移动, 跟随路径
                self.db.execute("UPDATE tracks SET path=?, updated_at=? WHERE path=?",
                                (cands[0], now_iso(), path))
                self.db.log("info", "library", f"文件位置变了(已自动跟随): {path} → {cands[0]}")
                by_base.pop(path.rsplit("/", 1)[-1].casefold(), None)
                stats["moved"] += 1
                continue
            self._handle_missing(row)
            self._missing_ids.add(int(row["id"]))
            stats["missing"] += 1

        # ---------- 2) 库里多出来的文件 -> 纳管(永不删除) ----------
        # 用户可能用别的工具往库里放歌; 不纳管的话去重看不见, 会重复下载。
        if self.cfg.sync.adopt_external and not self.dry_run:
            remaining = sorted(found - self.db.active_paths())
            if remaining:
                stats["adopted"] = self._adopt_external(remaining)

        stats["orphans_cleaned"] = self.db.cleanup_orphans()

        for row in self.db.tracks_by_status("missing"):
            if row["path"] and row["path"] in found:
                self.db.execute(
                    "UPDATE tracks SET status='active', updated_at=? WHERE id=?",
                    (now_iso(), row["id"]),
                )
                stats["restored"] += 1

        # ---------- 3) 长期失败的曲目定期给一次重试机会 ----------
        if self.cfg.sync.retry_failed_hours > 0 and not self.dry_run:
            stats["retry_reset"] = self._reset_stale_failures(self.cfg.sync.retry_failed_hours)
        return stats

    def _adopt_external(self, rel_paths: list[str], limit: int = 200) -> int:
        """把数据库不知道的库内文件纳管为 managed=0(原有文件, 任何删除逻辑都不碰)."""
        from .library_import import audio_score, read_tags

        added = 0
        for rel in rel_paths[:limit]:
            full = self.cfg.music_dir / rel
            try:
                tags = read_tags(full)
                if not tags:
                    continue
                _score, quality = audio_score(full)
                self.db.upsert_track({
                    "provider": "local", "remote_id": rel,
                    "title": tags["title"], "artists": tags["artists"],
                    "album": tags["album"], "albumartist": tags["albumartist"],
                    "year": tags["year"], "duration": tags["duration"],
                    "ext": full.suffix.lstrip("."), "path": rel,
                    "size": full.stat().st_size, "managed": 0, "status": "active",
                    "quality": quality or "local", "source": "local",
                })
                added += 1
            except Exception:  # noqa: BLE001 - 单个文件坏了不能拖垮整轮
                continue
        if added:
            self.db.log("info", "library",
                        f"纳管库内新增文件 {added} 个（标记为原有文件, 不会被删除）")
        return added

    def _reset_stale_failures(self, hours: int, limit: int = 20) -> int:
        """失败超过 N 小时的曲目重置 attempts, 给一次重试机会(音源可能后来上架)."""
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S%z",
                               time.localtime(time.time() - hours * 3600))
        rows = self.db.query(
            """SELECT id FROM tracks
               WHERE status='failed' AND attempts>=3 AND updated_at < ?
               ORDER BY updated_at LIMIT ?""", (cutoff, limit),
        )
        if not rows:
            return 0
        with self.db.tx() as conn:
            for r in rows:
                conn.execute("UPDATE tracks SET attempts=0, updated_at=? WHERE id=?",
                             (now_iso(), r["id"]))
        self.db.log("info", "download",
                    f"重置 {len(rows)} 首失败曲目（失败已超 {hours} 小时）, 本轮再试一次")
        return len(rows)

    def _handle_missing(self, row: dict) -> None:
        """本地文件消失 -> 标记所有同路径曲目为 missing, 并回推取消收藏."""
        siblings = self._siblings(row)
        ids = [r["id"] for r in siblings] or [row["id"]]
        with self.db.tx() as conn:
            conn.executemany(
                "UPDATE tracks SET status='missing', updated_at=? WHERE id=?",
                [(now_iso(), i) for i in ids],
            )
        for sib in siblings or [row]:
            self.db.log("info", "library", f"本地文件已删除: {sib['title']} - {sib['artists']}",
                        provider=sib["provider"], remote_id=sib["remote_id"])
            # 保护: 这首歌在库里还有别的副本(另一个专辑版本/另一份文件)时,
            # 只是"删掉了一个副本", 不该去平台取消收藏
            keep_copy = self._still_in_library(sib, row.get("path") or "")
            if not self.cfg.sync.push_unfavorite:
                self.notify.track_removed("本地文件已删除", sib["artists"], sib["title"],
                                          sib["path"])
                continue
            if keep_copy:
                self.db.log("info", "library",
                            f"跳过取消收藏（库里还有这首歌的其他副本）: {sib['title']}")
                self.notify.track_removed("本地文件已删除（库里还有副本，未取消收藏）",
                                          sib["artists"], sib["title"], sib["path"])
                continue
            # 找出引用过这首歌的歌单所属账号, 向对应账号回推取消收藏。
            # 专辑/榜单是单向来源(不算收藏), 从那里"移除"不代表用户不要这首歌
            accounts = [r["account"] or "" for r in self.db.query(
                """SELECT DISTINCT p.account FROM playlist_items pi
                   JOIN playlists p ON p.id = pi.playlist_id
                   WHERE pi.track_id = ?
                     AND COALESCE(p.kind,'playlist') NOT IN ('album','chart')""",
                (sib["id"],)
            )]
            # 不属于任何歌单/收藏的曲目(面板搜歌下载、库内纳管)在平台侧本来就没有收藏关系,
            # 回推"取消收藏"等于擅自去动用户的账号, 必须跳过
            if not accounts:
                self.db.log("info", "library",
                            f"跳过取消收藏（该曲目不来自任何歌单/收藏）: {sib['title']}")
                self.notify.track_removed("本地文件已删除（非歌单曲目，未回推平台）",
                                          sib["artists"], sib["title"], sib["path"])
                continue
            queued = 0
            for acc in accounts or [""]:
                if self.dry_run:
                    self.db.log("info", "library",
                                f"[试运行] 将取消收藏: {sib['provider']}/{acc or '默认'} / {sib['title']}")
                    continue
                self.db.enqueue_action("unfavorite", sib["provider"], {
                    "remote_id": sib["remote_id"],
                    "title": sib["title"],
                    "artists": sib["artists"],
                    "account": acc,
                    "reason": "local_file_deleted",
                })
                queued += 1
            if queued:
                self.notify.track_removed("本地文件已删除，已排队取消平台收藏",
                                          sib["artists"], sib["title"], sib["path"])
            else:
                self.notify.track_removed("本地文件已删除", sib["artists"], sib["title"],
                                          sib["path"])

    # ---------- 同曲洗版 ----------
    TRASH_DIR = ".musehub_trash"   # 点开头目录, Navidrome/MuseHub 都不扫描

    def trash_stats(self) -> dict:
        """回收目录占用情况(按日期分组)."""
        root = self.cfg.music_dir / self.TRASH_DIR
        files = 0
        size = 0
        by_day: dict[str, dict] = {}
        if root.exists():
            for f in root.rglob("*"):
                if not f.is_file():
                    continue
                files += 1
                fs = f.stat().st_size
                size += fs
                day = f.parent.name if f.parent != root else ""
                slot = by_day.setdefault(day, {"day": day, "files": 0, "bytes": 0})
                slot["files"] += 1
                slot["bytes"] += fs
        return {"files": files, "bytes": size,
                "by_day": sorted(by_day.values(), key=lambda x: x["day"], reverse=True)}

    def trash_empty(self) -> dict:
        """清空回收目录(真正删除). 洗版后确认没问题再清, 省空间."""
        root = self.cfg.music_dir / self.TRASH_DIR
        files = 0
        size = 0
        if root.exists():
            for f in root.rglob("*"):
                if f.is_file():
                    files += 1
                    size += f.stat().st_size
            shutil.rmtree(root, ignore_errors=True)
        if files:
            self.db.log("info", "dedup",
                        f"已清空回收目录: {files} 个文件, 释放 {size // 1_000_000} MB")
            self.notify.dedup_note(f"回收目录已清空：{files} 个文件，释放 {size // 1_000_000} MB")
        return {"files": files, "bytes": size}

    def _dispose_file(self, rel_path: str) -> tuple[bool, int]:
        """洗版时处理"不要了"的文件: 按设置移入回收目录 或 直接删除."""
        if self.cfg.sync.trash_keep:
            return self._trash_file(rel_path)
        src = self.cfg.music_dir / rel_path
        if not src.exists():
            return False, 0
        size = src.stat().st_size
        try:
            src.unlink()
            lrc = src.with_suffix(".lrc")
            if lrc.exists():
                lrc.unlink()
            return True, size
        except OSError as exc:
            self.db.log("warn", "dedup", f"删除失败 {rel_path}: {exc}")
            return False, 0

    def _trash_file(self, rel_path: str) -> tuple[bool, int]:
        """把库内文件(含同名 .lrc)移入回收目录. 返回 (是否移动, 字节数)."""
        src = self.cfg.music_dir / rel_path
        if not src.exists():
            return False, 0
        size = src.stat().st_size
        dest_dir = self.cfg.music_dir / self.TRASH_DIR / time.strftime("%Y%m%d")
        dest = dest_dir / rel_path.replace("/", "__")
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            lrc = src.with_suffix(".lrc")
            if lrc.exists():
                try:
                    shutil.move(str(lrc), dest_dir / (dest.stem + ".lrc"))
                except OSError:
                    pass
            return True, size
        except OSError as exc:
            self.db.log("warn", "dedup", f"移入回收失败 {rel_path}: {exc}")
            return False, 0

    def _repoint_records(self, old_path: str, new_path: str,
                         size: int = 0, ext: str = "", quality: str = "",
                         managed: int = 1) -> int:
        """把所有引用 old_path 的活跃记录改指 new_path.

        这是洗版的安全核心: 记录保持 active, 只是换了个文件 —— 不会被判定
        "本地缺失", 因此绝不会回推取消平台收藏。
        """
        rows = self.db.query(
            "SELECT id FROM tracks WHERE path=? AND status='active'", (old_path,))
        if not rows:
            return 0
        with self.db.tx() as conn:
            for r in rows:
                conn.execute(
                    """UPDATE tracks SET path=?, size=?, ext=?, quality=?, managed=?,
                       updated_at=? WHERE id=?""",
                    (new_path, size, ext, quality, managed, now_iso(), r["id"]))
        return len(rows)

    def dedupe_apply(self, keep: str = "first", group_index: int | None = None) -> dict:
        """同曲洗版: 每组保留最好(或最早入库)的文件, 其余移入回收目录.

        安全设计:
        1. 被替换的**所有记录**改指保留文件(status 保持 active), 绝不删记录 ——
           记录若变 deleted/missing 会被判定"本地缺失"而回推取消平台收藏。
        2. 差文件移入 .musehub_trash/<日期>/ 而不是直接删 —— 点开头目录
           Navidrome/MuseHub 都不扫描, 效果等同删除, 但随时可找回/可清空。
        3. 保留文件可能是用户原有文件(managed=0), 改指的记录继承它的归属。
        """
        from .library_import import audio_score
        groups = self.find_duplicates()
        if group_index is not None:
            groups = groups[group_index:group_index + 1] \
                if 0 <= group_index < len(groups) else []
        stats = {"groups": 0, "removed_files": 0, "repointed": 0,
                 "skipped": 0, "bytes_freed": 0}
        for g in groups:
            tracks = g["tracks"]
            if len(tracks) < 2:
                continue
            if keep == "best":
                scored = []
                for t in tracks:
                    score, _q = audio_score(self.cfg.music_dir / t["path"])
                    scored.append((score, t))
                winner = max(scored, key=lambda x: x[0])[1]
            else:
                winner = tracks[0]
            keep_path = winner["path"]
            stats["groups"] += 1
            for t in tracks:
                old_path = t["path"]
                if old_path == keep_path:
                    continue
                moved = self._repoint_records(
                    old_path, keep_path, int(winner["size"] or 0), winner["ext"],
                    winner["quality"], int(winner["managed"] or 0))
                if not moved:
                    stats["skipped"] += 1
                    continue
                stats["repointed"] += moved
                ok, size = self._dispose_file(old_path)
                if ok:
                    stats["removed_files"] += 1
                    stats["bytes_freed"] += size
                    self.db.log("info", "dedup",
                                f"洗版: {g['title']} — {g['artist']} 保留 {keep_path}; "
                                f"差音质移入回收 {old_path}")
        if stats["groups"]:
            self.db.log("info", "dedup",
                        f"洗版完成: 处理 {stats['groups']} 组, 移除 {stats['removed_files']} 个文件"
                        f"（释放 {stats['bytes_freed'] // 1_000_000} MB）,"
                        f"记录改指 {stats['repointed']} 条")
        return stats

    def _still_in_library(self, row: dict, exclude_path: str = "") -> bool:
        """同一首歌在库里是否还有别的存活副本(用来避免误取消平台收藏)."""
        key = (self._norm(row.get("title")),
               self._norm((row.get("artists") or "").split(";")[0]))
        skip = exclude_path or row.get("path") or ""
        for p in self._lib_index.get(key, []):
            if p == skip:
                continue
            if (self.cfg.music_dir / p).exists():
                return True
        return False

    def _siblings(self, row: dict) -> list[dict]:
        """找出"同一首歌"的所有记录: 同路径优先, 再按标题+艺人+时长匹配."""
        out: dict[int, dict] = {}
        path = row.get("path") or ""
        if path:
            for r in self.db.query(
                "SELECT * FROM tracks WHERE path=? AND status<>'deleted'", (path,)
            ):
                out[int(r["id"])] = dict(r)
        if not out:
            out[int(row["id"])] = row
        # 另一平台上的同一首歌(可能因故没下载成功)
        title = (row.get("title") or "").casefold().strip()
        artist = (row.get("artists") or "").split(";")[0].casefold().strip()
        duration = int(row.get("duration") or 0)
        if title and artist:
            for r in self.db.query(
                "SELECT * FROM tracks WHERE title<>'' AND status IN ('failed','pending','missing')"
            ):
                if r["provider"] == row["provider"]:
                    continue
                if (r["title"] or "").casefold().strip() != title:
                    continue
                if (r["artists"] or "").split(";")[0].casefold().strip() != artist:
                    continue
                if duration and r["duration"] and abs(int(r["duration"]) - duration) > 5:
                    continue
                out[int(r["id"])] = dict(r)
        return list(out.values())

    # ---------- 2. 拉取歌单 ----------
    def pull_playlists(self) -> dict:
        stats = {"playlists": 0, "remote_tracks": 0, "new_tracks": 0,
                 "removed_from_playlist": 0, "deleted": 0, "playlist_errors": 0}
        for pl in self.db.subscribed_playlists():
            provider = self.provider_for(pl["provider"], pl.get("account") or "")
            if provider is None:
                continue
            kind = (pl.get("kind") or "playlist")
            try:
                # 订阅类型: 歌单 / 专辑 / 榜单, 三种都归一成 RemotePlaylist
                if kind == "album":
                    remote = provider.album(pl["remote_id"])
                elif kind == "chart":
                    remote = provider.chart(pl["remote_id"])
                else:
                    remote = provider.playlist(pl["remote_id"])
            except Exception as exc:  # noqa: BLE001
                stats["playlist_errors"] += 1
                self.db.execute(
                    "UPDATE playlists SET last_error=?, last_sync_at=? WHERE id=?",
                    (str(exc)[:500], now_iso(), pl["id"]),
                )
                label = {"album": "专辑", "chart": "榜单"}.get(kind, "歌单")
                self.db.log("error", pl["provider"], f"{label}拉取失败 [{pl['name']}]: {exc}")
                continue

            old_ids = set(self.db.playlist_track_ids(pl["id"]))
            # 安全闸: 远端突然返回空, 但本地记录里明明有歌 -> 视为接口异常,
            # 绝不因此清空歌单成员、更不会连带删文件
            if not remote.tracks and old_ids and remote.declared_count != 0:
                stats["playlist_errors"] += 1
                msg = "远端返回 0 首, 与本地记录不符, 已跳过本次以免误删"
                self.db.execute(
                    "UPDATE playlists SET last_error=?, last_sync_at=? WHERE id=?",
                    (msg, now_iso(), pl["id"]),
                )
                self.db.log("warn", pl["provider"], f"歌单 {pl['name']}: {msg}")
                continue

            new_ids: list[int] = []
            for t in remote.tracks:
                row = t.to_row()
                existing = self.db.find_track(provider.name, str(t.remote_id))
                row["account"] = pl.get("account") or ""
                # 只有原本不是"搜歌入库"的记录才标成 playlist, 避免覆盖来源
                if not existing or not existing.get("source"):
                    row["source"] = "playlist"
                if existing is None:
                    tid = self.db.upsert_track({**row, "status": "pending"})
                    stats["new_tracks"] += 1
                else:
                    if existing["status"] in ("deleted", "missing") \
                            and int(existing["id"]) not in self._missing_ids \
                            and not self._has_pending_unfavorite(provider.name, str(t.remote_id)):
                        # 之前被删过/文件没了, 现在又出现在歌单里 -> 恢复下载
                        self.db.execute(
                            """UPDATE tracks SET status='pending', attempts=0, fail_reason='',
                               updated_at=? WHERE id=?""",
                            (now_iso(), existing["id"]),
                        )
                    tid = self.db.upsert_track(row)
                new_ids.append(tid)

            # 专辑/榜单是"单向来源", 不是用户收藏: 内容只会追加, 永不删除。
            # 榜单每期都在变, 若按歌单镜像规则处理, "跌出榜单"会被当成"平台删歌",
            # 进而删本地文件甚至回推取消收藏 —— 用户从来没收藏过这些歌, 这是错的。
            one_way = (pl.get("kind") or "playlist") in ("album", "chart")
            if one_way:
                # 合并: 新榜单内容在前, 曾经下过的歌保留在后面(本地与播放器歌单都不缩水)
                merged = list(dict.fromkeys(list(new_ids) + list(old_ids)))
                self.db.set_playlist_items(pl["id"], merged)
                new_set = set(new_ids)
                dropped = sum(1 for i in old_ids if i not in new_set)
            else:
                self.db.set_playlist_items(pl["id"], new_ids)
                dropped = 0
            self.db.execute(
                """UPDATE playlists SET name=?, cover=?, track_count=?, last_sync_at=?,
                   last_error='' WHERE id=?""",
                (remote.name or pl["name"], remote.cover or pl["cover"],
                 len(new_ids), now_iso(), pl["id"]),
            )
            stats["playlists"] += 1
            stats["remote_tracks"] += len(new_ids)

            # 远端已移除的曲目(仅普通歌单; 专辑/榜单永不删)
            removed = [] if one_way else list(old_ids - set(new_ids))
            added = set(new_ids) - old_ids
            if added or removed:
                acct = (pl.get("account") or "").strip()
                label = f"{self._provider_label(pl['provider'])}"
                if acct and acct != "默认":
                    label += f"·{acct}"
                self.notify.playlist_change(f"{label} {pl['name']}", len(added), len(removed))
            if dropped:
                kind_label = "专辑" if pl.get("kind") == "album" else "榜单"
                self.db.log("info", pl["provider"],
                            f"{kind_label} [{pl['name']}] 更新: {dropped} 首已不在最新内容里, "
                            f"本地文件与播放器歌单保留(单向来源不删歌)")
            for tid in removed:
                stats["removed_from_playlist"] += 1
                if self.db.track_ref_count(tid) == 0:
                    if self._maybe_delete(tid, reason="removed_from_playlist"):
                        stats["deleted"] += 1
        return stats

    # ---------- 3. 下载 ----------
    def download_pending(self) -> dict:
        stats = {"downloaded": 0, "download_failed": 0, "download_skipped": 0,
                 "dedup_reused": 0}
        limit = self.cfg.sync.max_downloads_per_cycle
        rows = self.db.query(
            """SELECT t.* FROM tracks t
               WHERE t.status IN ('pending','failed') AND t.attempts < 3
                 AND EXISTS (SELECT 1 FROM playlist_items pi
                             JOIN playlists p ON p.id=pi.playlist_id
                             WHERE pi.track_id=t.id AND p.subscribed=1)
               ORDER BY t.id LIMIT ?""",
            (limit,),
        )
        if not rows:
            return stats
        if self.dry_run:
            for r in rows[:10]:
                self.db.log("info", "download", f"[试运行] 待下载: {r['artists']} - {r['title']}")
            if len(rows) > 10:
                self.db.log("info", "download", f"[试运行] 另有 {len(rows) - 10} 首待下载(未逐条列出)")
            stats["download_skipped"] = len(rows)
            return stats

        workers = max(1, self.cfg.sync.download_concurrency)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._download_one, dict(r)): dict(r) for r in rows}
            for fut in as_completed(futures):
                row = futures[fut]
                try:
                    ok, reason = fut.result()
                except Exception as exc:  # noqa: BLE001
                    ok, reason = False, str(exc)
                if ok:
                    # 复用已有文件不算"下载", 单独计数, 免得面板数字虚高
                    if reason == "__dedup__":
                        stats["dedup_reused"] += 1
                    else:
                        stats["downloaded"] += 1
                else:
                    stats["download_failed"] += 1
                    self.db.execute(
                        "UPDATE tracks SET status='failed', fail_reason=?, attempts=attempts+1, updated_at=? WHERE id=?",
                        (reason[:500], now_iso(), row["id"]),
                    )
                    self.db.log("warn", row["provider"],
                                f"下载失败 {row['artists']} - {row['title']}: {reason}")
                    self.notify.track_failed(row["provider"], row["artists"],
                                             row["title"], reason)
        return stats

    def _download_one(self, row: dict) -> tuple[bool, str]:
        provider = self.provider_for(row["provider"], row.get("account") or "")
        if provider is None:
            return False, f"未知平台 {row['provider']}"
        track = RemoteTrack(
            provider=row["provider"], remote_id=row["remote_id"], title=row["title"],
            artists=[a.strip() for a in (row["artists"] or "").split(";") if a.strip()],
            album=row["album"], album_artist=row["albumartist"],
            duration=row["duration"], year=row["year"], track_no=row["track_no"],
            cover_url=row.get("cover") or "",
        )
        tid = row["id"]
        # 同曲去重: 库里已有同一首歌就直接复用文件
        dup = self._find_dedup_target(row)
        if dup is not None:
            # 关键: 继承目标文件的归属。复用用户原有文件时必须保持 managed=0,
            # 否则将来平台取消收藏会把用户的原文件删掉
            self.db.execute(
                """UPDATE tracks SET path=?, size=?, ext=?, quality=?, status='active',
                   managed=?, fail_reason='', attempts=0, updated_at=? WHERE id=?""",
                (dup["path"], dup["size"], dup["ext"], dup["quality"],
                 int(dup["managed"] or 0), now_iso(), tid),
            )
            self.db.log("info", row["provider"],
                        f"同曲去重: {row['title']} 复用已有文件 {dup['path']}")
            self.notify.track_added(row["provider"], row["artists"], row["title"],
                                    dup["quality"] or "", dup["path"],
                                    info={"cover": row.get("cover"),
                                          "album": row.get("album"),
                                          "year": row.get("year"),
                                          "duration": row.get("duration"),
                                          "ext": dup.get("ext"),
                                          "size": dup.get("size")})
            return True, "__dedup__"
        started = time.time()
        self.progress.start(tid, provider.display, row["title"], row["artists"])
        try:
            src_path, src = self.downloader.fetch(
                row["provider"], track,
                on_progress=lambda d, t: self.progress.progress(tid, d, t),
            )
        except DownloadError as exc:
            # 本平台所有音质档位都拿不到 -> 尝试其他平台搜同曲下载
            fb = self._fallback_fetch(row, track)
            if fb is None:
                self.progress.finish(tid, ok=False, error=str(exc))
                self.db.log_download(
                    row["provider"], row["remote_id"], row["title"], row["artists"],
                    "fail", error=str(exc), elapsed_ms=int((time.time() - started) * 1000))
                return False, str(exc)
            src_path, src = fb
            self.db.log("info", row["provider"],
                        f"跨平台回退: {track.title} 本平台无音源, 已从其他平台下载"
                        f" [{src.quality}]")
        except Exception as exc:  # noqa: BLE001
            self.progress.finish(tid, ok=False, error=str(exc))
            raise

        self.progress.update(tid, state="tagging", quality=src.quality)
        meta = TrackMeta(
            title=track.title or "未知歌名",
            artists=track.artists or ["未知艺术家"],
            album=track.album,
            albumartist=track.album_artist,
            year=track.year,
            track_no=track.track_no,
            duration=track.duration,
        )
        cover = b""
        try:
            self.organizer.write_tags(src_path, meta)
            cover = self.downloader.fetch_image(track.cover_url) if track.cover_url else b""
            if cover and self.cfg.organizer.embed_cover:
                self.organizer.embed_cover(src_path, cover)
            target = self.organizer.place(src_path, meta, self.cfg.music_dir)
        except Exception as exc:  # noqa: BLE001
            try:
                src_path.unlink(missing_ok=True)
            except OSError:
                pass
            self.progress.finish(tid, ok=False, error=f"归档失败: {exc}", quality=src.quality)
            self.db.log_download(
                row["provider"], row["remote_id"], row["title"], row["artists"],
                "fail", quality=src.quality, ext=src.ext,
                elapsed_ms=int((time.time() - started) * 1000), error=f"归档失败: {exc}")
            return False, f"归档失败: {exc}"

        if cover:
            self.organizer.save_cover_file(target, cover)
        if self.cfg.organizer.write_lyrics_file:
            try:
                lyric = provider.lyric(track)
                if lyric:
                    self.organizer.save_lyric(target, lyric)
            except Exception:  # noqa: BLE001 - 歌词失败不算失败
                pass

        rel = target.relative_to(self.cfg.music_dir).as_posix()
        self.db.execute(
            """UPDATE tracks SET path=?, size=?, ext=?, quality=?, status='active',
               managed=1, fail_reason='', attempts=0, updated_at=? WHERE id=?""",
            (rel, target.stat().st_size if target.exists() else 0,
             target.suffix.lstrip("."), src.quality, now_iso(), row["id"]),
        )
        # 自动洗版: 同曲已有别的文件时, 只留音质更好的那个(差的进回收目录)
        kept = self._auto_upgrade(row, rel)
        if kept:
            self.db.log("info", row["provider"],
                        f"已入库(复用库里更优版本): {track.artist_str} - {track.title}",
                        path=kept)
        else:
            self.db.log("info", row["provider"],
                        f"已下载: {track.artist_str} - {track.title} [{src.quality}]",
                        path=rel)
        elapsed = int((time.time() - started) * 1000)
        self.progress.finish(tid, ok=True, quality=src.quality)
        self.db.log_download(
            row["provider"], row["remote_id"], row["title"], row["artists"],
            "ok", quality=src.quality, ext=target.suffix.lstrip("."),
            size=target.stat().st_size if target.exists() else 0, elapsed_ms=elapsed)
        self.notify.track_added(row["provider"], track.artist_str, track.title,
                                src.quality, kept or rel,
                                info={"cover": row.get("cover") or track.cover_url,
                                      "album": track.album or row.get("album"),
                                      "year": track.year or row.get("year"),
                                      "duration": track.duration or row.get("duration"),
                                      "ext": target.suffix.lstrip("."),
                                      "size": target.stat().st_size if target.exists() else 0})
        return True, ""

    def _has_pending_unfavorite(self, provider: str, remote_id: str) -> bool:
        """该曲还有没发出去的取消收藏动作时不复活, 避免下载/删除来回震荡."""
        for a in self.db.query(
            "SELECT payload FROM actions WHERE kind='unfavorite' AND provider=? "
            "AND state IN ('queued','running','failed')", (provider,)
        ):
            if str((a["payload"] or {}).get("remote_id")) == str(remote_id):
                return True
        return False

    # ---------- 同曲去重 ----------
    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"[\s\W_]+", "", (s or "").casefold())

    # 平台音质标签 -> 典型文件的音频质量分(与 audio_score 同一套算法).
    # 数值取该档位的典型参数: 无损 16bit/44.1k = 1_000_000+160_000+44_100
    EXPECT_SCORE = {
        "jymaster": 1_700_000,   # 24bit/192k
        "sb00": 1_700_000, "q000": 1_600_000, "ai00": 1_400_000,
        "rs01": 1_336_000, "hires": 1_336_000,      # 24bit/96k
        "sky": 1_300_000, "jyeffect": 1_250_000,
        "lossless": 1_204_100, "f000": 1_204_100, "a000": 1_204_100,
        "exhigh": 320_000, "m800": 320_000, "c400": 256_000,
        "higher": 192_000, "m500": 128_000, "standard": 128_000,
    }

    def _expected_score(self, provider_name: str, account: str = "") -> int:
        """本次下载"最好能拿到"的质量分(按该平台设置的音质上限估算)."""
        provider = self.provider_for(provider_name, account)
        if provider is None:
            return 0
        max_q = str(getattr(provider.cfg, "max_quality", "") or "")
        if not max_q:
            # 有的 provider(测试用 mock)直接持有整体 Config
            section = getattr(provider.cfg, provider_name, None)
            max_q = str(getattr(section, "max_quality", "") or "")
        return self.EXPECT_SCORE.get(max_q, 0)

    def _same_song_rows(self, row: dict) -> list[dict]:
        """库里同曲(歌名+主艺人+时长±5s)的其他活跃记录."""
        title = self._norm(row.get("title"))
        artist = self._norm((row.get("artists") or "").split(";")[0])
        if not title or not artist:
            return []
        dur = int(row.get("duration") or 0)
        out = []
        for r in self.db.query(
            "SELECT * FROM tracks WHERE status='active' AND path<>'' AND id<>?", (row["id"],)
        ):
            if self._norm(r["title"]) != title:
                continue
            if self._norm((r["artists"] or "").split(";")[0]) != artist:
                continue
            if dur and r["duration"] and abs(int(r["duration"]) - dur) > 5:
                continue
            if (self.cfg.music_dir / r["path"]).exists():
                out.append(dict(r))
        return out

    def _fallback_fetch(self, row: dict, track) -> tuple | None:
        """本平台无音源时, 用其他平台搜索同曲并下载.

        匹配很严: 歌名、主艺人完全一致(规范化后) 且 时长±5s —— 防止拿到
        翻唱/Live/remix。多账号平台只试第一账号的搜索。
        """
        if not self.cfg.sync.fallback_platform:
            return None
        title_n = self._norm(track.title)
        artist_n = self._norm(track.artists[0] if track.artists else "")
        if not title_n or not artist_n:
            return None
        seen: set[str] = {row["provider"]}
        for name, provider in self.providers.items():
            platform = name.split(":")[0]
            if platform in seen:
                continue
            seen.add(platform)
            try:
                hits = provider.search(
                    f"{track.artists[0] if track.artists else ''} {track.title}".strip(),
                    limit=5)
            except Exception:  # noqa: BLE001 - 搜索失败不影响其他平台
                continue
            for t in hits:
                if self._norm(t.title) != title_n:
                    continue
                if self._norm((t.artist_str or "").split(";")[0]) != artist_n:
                    continue
                if track.duration and t.duration and \
                        abs(int(t.duration) - int(track.duration)) > 5:
                    continue
                try:
                    return self.downloader.fetch(name, t)
                except DownloadError:
                    continue
        return None

    def _find_dedup_target(self, row: dict) -> dict | None:
        """库里已有同曲文件时, 判断要不要直接复用(不下载).

        规则: 只有已有文件的**真实音质 >= 本次能下到的音质**才复用。
        已有更差时返回 None —— 让它去下载更好的版本, 下载完由 _auto_upgrade
        自动把差版本移入回收。这样"已经有更好的就不再下差的""有差的会升级成好的"。
        """
        if not self.cfg.sync.dedup:
            return None
        from .library_import import audio_score
        best: dict | None = None
        best_score = -1
        for r in self._same_song_rows(row):
            score, _q = audio_score(self.cfg.music_dir / r["path"])
            if score > best_score:
                best, best_score = r, score
        if best is None:
            return None
        expected = self._expected_score(row.get("provider", ""), row.get("account") or "")
        if expected and best_score < expected:
            return None      # 已有的是低音质, 值得下个更好的
        return best

    def _auto_upgrade(self, row: dict, new_rel: str) -> str:
        """下载入库后自动处理同曲多版本: 谁音质好用谁, 差的移入回收目录.

        返回 "" 表示新文件保留; 否则返回"最终保留的旧文件路径"(新文件已入回收)。
        """
        if not self.cfg.sync.dedup:
            return ""
        from .library_import import audio_score
        new_score, _q = audio_score(self.cfg.music_dir / new_rel)
        if new_score < 0:
            return ""
        best: dict | None = None
        best_score = -1
        for r in self._same_song_rows(row):
            if r["path"] == new_rel:
                continue
            score, _q2 = audio_score(self.cfg.music_dir / r["path"])
            if score > best_score:
                best, best_score = r, score
        if best is None:
            return ""

        if best_score >= new_score:
            # 新下的不如已有的 -> 新文件不入库, 记录改指已有文件
            self._repoint_records(new_rel, best["path"], int(best["size"] or 0),
                                  best["ext"], best["quality"],
                                  int(best["managed"] or 0))
            ok, size = self._dispose_file(new_rel)
            if ok:
                self.db.log("info", "dedup",
                            f"自动洗版: {row['title']} 新下载音质不占优, 已弃用并复用 "
                            f"{best['path']}")
                self.notify.dedup_note(f"{row['title']} 已有更好版本，新下载未采用")
            return best["path"]

        # 新的更好 -> 旧记录全部改指新文件, 旧文件移入回收
        for r in self._same_song_rows(row):
            if r["path"] == new_rel:
                continue
            self._repoint_records(r["path"], new_rel)
            ok, size = self._dispose_file(r["path"])
            if ok:
                self.db.log("info", "dedup",
                            f"自动洗版: {row['title']} 升级为更好音质, 旧版本移入回收 "
                            f"{r['path']}")
                self.notify.dedup_note(f"{row['title']} 已升级为更好音质，旧版本入回收")
        return ""

    def find_duplicates(self) -> list[dict]:
        """同曲多文件: 按 (歌名+主艺人) 分组, 组内按**不同文件**去重.

        关键: 多条记录指向同一个文件(去重复用、洗版之后)不算重复 —— 否则
        洗版完列表里还能看到它, 让人以为没生效; 而且"7 首歌只移走 1 个文件"
        也会让人困惑(其实是 7 条记录共用 2 个文件)。
        """
        rows = [dict(r) for r in self.db.query(
            "SELECT * FROM tracks WHERE status='active' AND path<>'' ORDER BY id"
        )]
        by_path: dict[str, dict] = {}
        refs: dict[str, int] = {}
        for r in rows:
            p = r["path"]
            refs[p] = refs.get(p, 0) + 1
            cur = by_path.get(p)
            # 代表记录优先取 managed=1(自己下载的), 展示的音质更有参考意义
            if cur is None or (int(r["managed"] or 0) == 1 and int(cur["managed"] or 0) == 0):
                by_path[p] = r

        groups: dict[tuple[str, str], list[dict]] = {}
        for p, r in by_path.items():
            artist = (r.get("artists") or "").split(";")[0]
            key = (self._norm(r.get("title")), self._norm(artist))
            if key[0] and key[1]:
                groups.setdefault(key, []).append(r)

        out = []
        for _, items in groups.items():
            if len(items) < 2:
                continue
            out.append({
                "title": items[0]["title"],
                "artist": items[0]["artists"].split(";")[0],
                "tracks": [{
                    "id": r["id"], "provider": r["provider"], "remote_id": r["remote_id"],
                    "album": r["album"], "quality": r["quality"], "ext": r["ext"],
                    "size": r["size"], "path": r["path"], "managed": int(r["managed"] or 0),
                    "refs": refs.get(r["path"], 1),
                } for r in items],
            })
        out.sort(key=lambda g: -len(g["tracks"]))
        return out

    # ---------- 删除判定 ----------
    def _maybe_delete(self, track_id: int, reason: str) -> bool:
        row = self.db.get_track(track_id)
        if not row or row["status"] != "active":
            return False
        if self.delete_policy == "never":
            self.db.log("info", "cleanup", f"[策略 never] 保留: {row['title']}")
            return False
        if int(row["managed"] or 0) != 1:
            self.db.log("info", "cleanup", f"保留用户原有文件: {row['path']}")
            return False
        if self.db.track_ref_count(track_id) > 0:
            return False
        # 同路径还有其他活跃记录(另一平台下载的同一首歌)且仍被引用 -> 不能删
        for sib in self.db.query(
            "SELECT * FROM tracks WHERE path=? AND id<>? AND status='active'", (row["path"], track_id)
        ):
            if self.db.track_ref_count(int(sib["id"])) > 0:
                self.db.execute("UPDATE tracks SET status='deleted', updated_at=? WHERE id=?",
                                (now_iso(), track_id))
                return False
        if self.dry_run:
            self.db.log("info", "cleanup", f"[试运行] 将删除: {row['path']} ({reason})")
            return False

        target = self.cfg.music_dir / row["path"]
        removed = self.organizer.remove_track_files(target)
        self.db.execute("UPDATE tracks SET status='deleted', updated_at=? WHERE id=?",
                        (now_iso(), track_id))
        if removed:
            self.db.log("info", "cleanup", f"已删除本地文件: {row['path']} ({reason})", files=removed)
            why = "平台已取消收藏" if reason == "removed_from_playlist" else reason
            self.notify.track_removed(why, row["artists"], row["title"], row["path"])
        return bool(removed)

    # ---------- 手动删除(面板里点删除) ----------
    def delete_track(self, track_id: int) -> dict:
        """删一首歌: 删文件 + 标记已移除 + 排队取消平台收藏, 并立刻推出去.

        为什么要"连坐"同路径的记录:
          去重之后一个音频文件可能被多个平台记录共用。文件一删, 这些记录全都指向
          不存在的路径。只把点的那条标记掉, 剩下的要等下一轮扫描才收尾, 期间
          mirror_player 还会拿着失效路径去同步歌单。所以这里一次性处理干净。

        为什么不用回收目录:
          洗版进回收是为了"后悔能找回"; 用户主动点删除就是要删掉, 放回收反而
          会占着空间让人以为没删掉。UI 上有二次确认。
        """
        row = self.db.get_track(track_id)
        if not row:
            return {"ok": False, "error": "曲目不存在"}
        if self.dry_run:
            return {"ok": False, "error": "当前是试运行模式, 不会真的删除"}
        path = (row["path"] or "").strip()
        if not path:
            return {"ok": False, "error": "这条记录没有关联文件"}
        if row["status"] != "active":
            return {"ok": False, "error": f"当前状态是「{row['status']}」, 没有可删的文件"}

        rows = self.db.query(
            "SELECT * FROM tracks WHERE path=? AND status='active'", (path,)) or [row]
        target = self.cfg.music_dir / path
        removed = self.organizer.remove_track_files(target) if target.exists() else []

        queued = 0
        for r in rows:
            self.db.execute("UPDATE tracks SET status='deleted', updated_at=? WHERE id=?",
                            (now_iso(), r["id"]))
            if not self.cfg.sync.push_unfavorite:
                continue
            accounts = [x["account"] or "" for x in self.db.query(
                """SELECT DISTINCT p.account FROM playlist_items pi
                   JOIN playlists p ON p.id = pi.playlist_id
                   WHERE pi.track_id = ?""", (r["id"],))] or [""]
            for acc in accounts:
                self.db.enqueue_action("unfavorite", r["provider"], {
                    "remote_id": r["remote_id"],
                    "title": r["title"],
                    "artists": r["artists"],
                    "account": acc,
                    "reason": "manual_delete_in_panel",
                })
                queued += 1

        self.db.log("info", "cleanup",
                    f"手动删除: {row['artists']} - {row['title']} ({path}), "
                    f"文件 {len(removed)} 个, 待取消收藏 {queued}")
        self.notify.track_removed("你在面板里删除了这首歌", row["artists"], row["title"], path)
        self.notify.flush_events()
        if queued and not self.dry_run:
            try:
                self.push_actions()
            except Exception as exc:  # noqa: BLE001 - 回推失败不影响文件已删的事实
                self.db.log("warn", "cleanup", f"删除后立即回推失败(下轮重试): {exc}")
        return {"ok": True, "files": len(removed), "queued": queued, "path": path}

    # ---------- 4. 回推平台 ----------
    def push_actions(self) -> dict:
        stats = {"actions_done": 0, "actions_failed": 0, "actions_pending": 0}
        if self.dry_run:
            pending = self.db.pending_actions(50)
            stats["actions_pending"] = len(pending)
            return stats
        actions = self.db.claim_actions(20)
        for act in actions:
            provider = self.provider_for(act["provider"],
                                         (act.get("payload") or {}).get("account", ""))
            if provider is None:
                self.db.finish_action(act["id"], False, f"未知平台 {act['provider']}")
                stats["actions_failed"] += 1
                continue
            try:
                if act["kind"] == "unfavorite":
                    payload = act["payload"]
                    track = RemoteTrack(
                        provider=act["provider"], remote_id=payload["remote_id"],
                        title=payload.get("title", ""),
                        artists=[payload.get("artists", "")],
                    )
                    provider.favorite(track, on=False)
                elif act["kind"] == "favorite":
                    payload = act["payload"]
                    track = RemoteTrack(
                        provider=act["provider"], remote_id=payload["remote_id"],
                        title=payload.get("title", ""),
                    )
                    provider.favorite(track, on=True)
                else:
                    raise Unsupported(f"未实现的动作 {act['kind']}")
                self.db.finish_action(act["id"], True)
                stats["actions_done"] += 1
                self.db.log("info", act["provider"],
                            f"已回推 {act['kind']}: {act['payload'].get('title', '')}")
            except Unsupported as exc:
                self.db.finish_action(act["id"], False, str(exc), retry_after=None)
                stats["actions_failed"] += 1
                self.db.log("warn", act["provider"],
                            f"回推失败(需人工): {act['payload'].get('title','')} - {exc}")
                self.notify.error(
                    "取消收藏回推失败（需人工处理）",
                    f"{act['payload'].get('title', '')} — {exc}")
            except Exception as exc:  # noqa: BLE001
                self.db.finish_action(act["id"], False, str(exc), retry_after=300)
                stats["actions_failed"] += 1
                self.db.log("warn", act["provider"], f"回推重试中: {exc}")
        return stats

    # ---------- 5. 播放器镜像 ----------
    def mirror_player(self) -> dict:
        stats = {"navidrome_playlists": 0, "navidrome_missing": 0, "navidrome_error": ""}
        if not self.navidrome.available():
            return stats
        if self.dry_run:
            stats["navidrome_error"] = "试运行模式跳过"
            return stats
        try:
            index = self.navidrome.index_songs()
        except Exception as exc:  # noqa: BLE001
            stats["navidrome_error"] = str(exc)
            self.db.log("warn", "navidrome", f"曲库索引失败: {exc}")
            return stats

        for pl in self.db.subscribed_playlists():
            rows = self.db.query(
                """SELECT t.path, t.provider FROM tracks t
                   JOIN playlist_items pi ON pi.track_id=t.id
                   WHERE pi.playlist_id=? AND t.status='active' AND t.path<>''
                   ORDER BY pi.position""",
                (pl["id"],),
            )
            paths = [r["path"] for r in rows]
            label = self._provider_label(pl["provider"])
            acct = (pl.get("account") or "").strip()
            if acct and acct != "默认":
                label = f"{label}·{acct}"   # 多账号同名歌单不能撞名
            # 用户改过名就用自定义名, 否则用平台歌单名
            show_name = (pl.get("display_name") or "").strip() or pl["name"]
            name = f"{self.cfg.navidrome.playlist_prefix}[{label}] {show_name}"
            # 指定了播放器账号就用该账号身份创建(私有歌单只有 owner 看得见)
            acct_name = (pl.get("mirror_account") or "").strip()
            client = self.navidrome
            if acct_name and acct_name != self.cfg.navidrome.username:
                pwd = (getattr(self, "nd_accounts", {}) or {}).get(acct_name)
                if not pwd:
                    self.db.log("warn", "navidrome",
                                f"播放器账号 {acct_name} 未配置密码, 跳过 [{name}]")
                    continue
                client = self.navidrome.for_account(acct_name, pwd)
            try:
                res = client.sync_playlist(name, paths, index)
                stats["navidrome_playlists"] += 1
                stats["navidrome_missing"] += int(res.get("missing", 0))
                self.db.execute(
                    "UPDATE playlists SET navidrome_id=?, navidrome_name=? WHERE id=?",
                    (res.get("playlist_id", ""), name, pl["id"]),
                )
            except Exception as exc:  # noqa: BLE001
                stats["navidrome_error"] = str(exc)[:200]
                self.db.log("warn", "navidrome", f"歌单同步失败 [{name}]: {exc}")
        return stats

    def _provider_label(self, name: str) -> str:
        p = self.providers.get(name)
        return p.display if p else name

    # ---------- 单曲下载(供 TG/Web 用) ----------

    # ---------- 音质洗版 ----------
    def lossy_tracks(self) -> list[dict]:
        """列出所有有损曲目(按名次升序, 最差的排前面)."""
        from .quality import is_lossy
        rows = [dict(r) for r in self.db.query(
            """SELECT id, provider, account, remote_id, title, artists, album, albumartist,
                      year, track_no, duration, cover, quality, path, size, ext
               FROM tracks WHERE status='active' AND path<>'' AND remote_id<>''
               ORDER BY id""")]
        out = [r for r in rows if is_lossy(r["quality"])]
        from .quality import rank
        out.sort(key=lambda r: (rank(r["quality"]), r["artists"], r["title"]))
        return out

    def upgrade_quality(self, ids: list[int] | None = None,
                        progress: dict | None = None) -> dict:
        """洗版: 重新按最高档下载, 换上更好的文件.

        只做"真的更好"的替换 —— 下回来发现档位没提升就把临时文件丢掉,
        不会为了洗版而白折腾一遍库。
        """
        from .quality import better, rank
        rows = self.lossy_tracks()
        if ids is not None:
            keep = {int(i) for i in ids}
            rows = [r for r in rows if int(r["id"]) in keep]

        out = {"total": len(rows), "upgraded": 0, "skipped": 0, "failed": 0,
               "no_source": 0, "samples": [], "errors": []}
        touched = 0
        for idx, row in enumerate(rows, 1):
            label = f"{row['artists']} - {row['title']}"
            if progress is not None:
                progress.update({"done": idx, "total": len(rows), "current": label})
            provider = self.provider_for(row["provider"], row.get("account") or "")
            if provider is None:
                out["failed"] += 1
                continue
            track = RemoteTrack(
                provider=row["provider"], remote_id=row["remote_id"], title=row["title"],
                artists=[a.strip() for a in (row["artists"] or "").split(";") if a.strip()],
                album=row["album"], album_artist=row["albumartist"],
                duration=int(row["duration"] or 0), year=row["year"],
                track_no=int(row["track_no"] or 0), cover_url=row.get("cover") or "",
            )
            try:
                src_path, src = self.downloader.fetch(row["provider"], track)
            except Exception as exc:  # noqa: BLE001 - 单曲失败不影响整批
                msg = str(exc)[:120]
                out["no_source" if "无可用音源" in msg else "failed"] += 1
                out["errors"].append(f"{label}: {msg}")
                continue

            if not better(src.quality, row["quality"]):
                try:
                    src_path.unlink(missing_ok=True)
                except OSError:
                    pass
                out["skipped"] += 1
                continue

            try:
                meta = TrackMeta(
                    title=track.title or "未知歌名",
                    artists=track.artists or ["未知艺术家"],
                    album=track.album or "",
                    albumartist=track.album_artist or "",
                    year=str(track.year or ""),
                    track_no=int(track.track_no or 0),
                    duration=int(track.duration or 0),
                )
                self.organizer.write_tags(src_path, meta)
                cover = (self.downloader.fetch_image(track.cover_url)
                         if track.cover_url else b"")
                if cover and self.cfg.organizer.embed_cover:
                    self.organizer.embed_cover(src_path, cover)
                target = self.organizer.place(src_path, meta, self.cfg.music_dir)
                if cover:
                    self.organizer.save_cover_file(target, cover)
            except Exception as exc:  # noqa: BLE001
                try:
                    src_path.unlink(missing_ok=True)
                except OSError:
                    pass
                out["failed"] += 1
                out["errors"].append(f"{label}: 归档失败 {str(exc)[:80]}")
                continue

            new_rel = target.relative_to(self.cfg.music_dir).as_posix()
            old_rel = row["path"]
            self.db.execute(
                """UPDATE tracks SET path=?, size=?, ext=?, quality=?, updated_at=? WHERE id=?""",
                (new_rel, target.stat().st_size if target.exists() else 0,
                 target.suffix.lstrip("."), src.quality, now_iso(), row["id"]),
            )
            if old_rel and old_rel != new_rel:
                self._dispose_file(old_rel)      # 旧文件进回收目录
            out["upgraded"] += 1
            touched += 1
            if len(out["samples"]) < 10:
                out["samples"].append(f"{label}: {row['quality']} → {src.quality}")
            self.db.log("info", row["provider"],
                        f"洗版: {label} {row['quality']} → {src.quality}", path=new_rel)

        if touched:
            try:
                self.mirror_player()
            except Exception as exc:  # noqa: BLE001 - 镜像失败不影响洗版结果
                self.db.log("warn", "quality", f"洗版后镜像失败: {exc}")
        self.db.log("info", "quality",
                    f"音质洗版完成: 升级 {out['upgraded']} / 已是最好 {out['skipped']} / "
                    f"无源 {out['no_source']} / 失败 {out['failed']}")
        return out

    def download_single(self, track: RemoteTrack) -> dict:
        """把一首搜到的歌直接下载进库(不依赖歌单).

        语义: 来源标记为 `search` —— 它不属于任何歌单, 因此
        - 平台取消收藏不会连带删它(它不在歌单里)
        - 删它的本地文件也不会回推取消收藏(平台侧本来就没收藏它)
        想让"本地有 = 平台收藏", 把「搜歌下载后自动收藏」打开即可。
        """
        if self.dry_run:
            return {"ok": False, "error": "当前是试运行模式, 请先在 Web 面板关闭试运行"}
        row = track.to_row()
        existing = self.db.find_track(track.provider, str(track.remote_id))
        if existing and existing["status"] == "active":
            return {"ok": True, "skipped": True, "title": track.title,
                    "path": existing["path"]}
        tid = self.db.upsert_track({**row, "status": "pending", "source": "search"})
        ok, reason = self._download_one({**row, "id": tid})
        if not ok:
            self.db.execute(
                "UPDATE tracks SET status='failed', fail_reason=?, attempts=attempts+1 WHERE id=?",
                (reason[:500], tid),
            )
            return {"ok": False, "error": reason, "title": track.title}

        favorited = ""
        if getattr(self.cfg.sync, "favorite_on_download", False):
            provider = self.provider_for(track.provider, "")
            if provider is None:
                favorited = "无可用账号"
            else:
                try:
                    provider.favorite(track, True)
                    favorited = "已收藏"
                    self.db.log("info", "search",
                                f"已回写平台收藏: {track.artist_str} - {track.title}")
                except Exception as exc:  # noqa: BLE001 - 收藏失败不能影响下载结果
                    favorited = f"收藏失败: {str(exc)[:120]}"
                    self.db.log("warn", "search",
                                f"回写收藏失败: {track.title} - {exc}")
        return {"ok": True, "title": track.title, "favorited": favorited}
