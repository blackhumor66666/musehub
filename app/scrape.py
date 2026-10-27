"""元数据刮削: 按平台 ID 补全缺失字段(年份 / 音轨号 / 专辑 / 专辑艺人 / 封面).

为什么自己做而不是用 Music Tag Web 这类工具:
  1. 我们自己下载的歌, 库里就有 `provider + remote_id`, 按 ID 取的是"就是这首歌",
     零误配; 外部工具只能"标题+艺人"搜索, 现场版/翻唱/同名片很容易配错。
  2. 不依赖任何第三方镜像/代理接口(那些站点寿命都不长)。
  3. 可以严格做到"只补空缺"——标题/艺人/专辑的写法是经过规范化的,
     绝不能被别的源的写法覆盖。

能力边界(诚实声明):
  - 流派(genre)补不了: 网易云/QQ 的接口都不返回流派, 酷狗/酷我虽然返回歌曲信息
    但不含流派字段。要流派只能靠人工或 MusicBrainz 这类需要另配的服务。
  - 只处理"有平台 ID"的记录; `provider='local'`(用户自己放进库的文件)没有 ID,
    需要搜索匹配, 误配风险高, 不在本轮自动处理范围内。
"""
from __future__ import annotations

from typing import Any, Iterable

from .db import now_iso
from .organizer import Organizer, TrackMeta

# 库里存得下、且能从平台权威数据补的字段
FIELDS = ("year", "track_no", "album", "albumartist", "cover")
FIELD_LABEL = {"year": "年份", "track_no": "音轨号", "album": "专辑",
               "albumartist": "专辑艺人", "cover": "封面"}


def _is_empty(row: dict, f: str) -> bool:
    v = row.get(f)
    if f == "track_no":
        try:
            return int(v or 0) <= 0
        except (TypeError, ValueError):
            return True
    return not str(v or "").strip()


def _has_platform_id(row: dict) -> bool:
    return row.get("provider") not in ("", "local") and bool(str(row.get("remote_id") or "").strip())


def scan(db: Any) -> dict:
    """只读体检: 哪些记录缺哪些字段, 其中多少能精确刮."""
    rows = [dict(r) for r in db.query(
        """SELECT id, provider, remote_id, title, artists, album, albumartist,
                  year, track_no, cover, path
           FROM tracks WHERE status='active' ORDER BY id""")]
    counts = {f: 0 for f in FIELDS}
    items: list[dict] = []
    exact = search_only = 0
    for r in rows:
        need = [f for f in FIELDS if _is_empty(r, f)]
        if not need:
            continue
        ok = _has_platform_id(r)
        if ok:
            exact += 1
        else:
            search_only += 1
        for f in need:
            counts[f] += 1
        items.append({
            "id": int(r["id"]), "title": r["title"], "artists": r["artists"],
            "path": r["path"], "provider": r["provider"],
            "missing": need, "exact": ok,
        })
    return {
        "total": len(rows), "need": len(items), "exact": exact,
        "search_only": search_only, "counts": counts,
        "items": items, "scanned_at": now_iso(),
    }


def _fetch_details(tasks: list[tuple[dict, list[str], Any]],
                   progress: dict | None = None) -> dict[int, Any]:
    """并发取每首歌的详情, 返回 {track_id: detail}.

    并发的是"网络等待", 数据库与文件写入仍由调用方串行执行。
    requests.Session 不是线程安全的, 所以每个线程用独立的 provider 副本
    (副本共享同一份配置与 db 连接, 只隔离 http 会话)。
    """
    import os
    import threading
    from concurrent.futures import ThreadPoolExecutor

    if not tasks:
        return {}
    try:
        workers = int(os.environ.get("SCRAPE_WORKERS", "4"))
    except ValueError:
        workers = 4
    workers = max(1, min(8, workers))

    local = threading.local()
    lock = threading.Lock()
    done = {"n": 0}
    total = len(tasks)

    def work(item: tuple[dict, list[str], Any]) -> tuple[int, Any]:
        row, _need, base = item
        cache = getattr(local, "cache", None)
        if cache is None:
            cache = local.cache = {}
        prov = cache.get(id(base))
        if prov is None:
            try:
                prov = type(base)(base.cfg, base.db)
            except Exception:  # noqa: BLE001 - 造不出副本就退回共享实例
                prov = base
            cache[id(base)] = prov
        try:
            detail = prov.track_detail(str(row["remote_id"]))
        except Exception:  # noqa: BLE001 - 单曲失败不影响其他
            detail = None
        with lock:
            done["n"] += 1
            if progress is not None:
                progress.update({"done": done["n"], "total": total,
                                 "current": f"{row['artists']} - {row['title']}"})
        return int(row["id"]), detail

    out: dict[int, Any] = {}
    if workers <= 1 or total < 4:
        for it in tasks:
            rid, detail = work(it)
            out[rid] = detail
        return out
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scrape") as pool:
            for rid, detail in pool.map(work, tasks):
                out[rid] = detail
    except Exception:  # noqa: BLE001 - 并发出问题就退回串行, 不能丢任务
        for it in tasks:
            rid, detail = work(it)
            out[rid] = detail
    return out


def _detail_value(detail: Any, field: str) -> Any:
    """把平台详情对象映射到库字段."""
    if field == "albumartist":
        return detail.album_artist or ""
    if field == "cover":
        return detail.cover_url or ""
    if field == "track_no":
        return int(detail.track_no or 0)
    return getattr(detail, field, "") or ""


def run(ctx: Any, ids: Iterable[int] | None = None, fields: Iterable[str] = FIELDS,
        write_files: bool = True, progress: dict | None = None,
        dry_run: bool = False) -> dict:
    """按平台 ID 补全缺失字段.

    write_files=True 时同时把新值写回音频标签(播放器才能看到)。
    dry_run=True 只统计"能补什么", 不动数据库也不动文件。
    """
    db = ctx.db
    want = [f for f in (fields or FIELDS) if f in FIELDS]
    rows = [dict(r) for r in db.query(
        """SELECT id, provider, remote_id, title, artists, album, albumartist,
                  year, track_no, cover, path, duration, account
           FROM tracks WHERE status='active' ORDER BY id""")]
    if ids is not None:
        keep = {int(i) for i in ids}
        rows = [r for r in rows if int(r["id"]) in keep]

    org = Organizer(ctx.cfg.organizer)
    stats: dict = {"scanned": 0, "updated": 0, "no_source": 0, "no_id": 0,
                   "skipped": 0, "failed": 0, "files_written": 0,
                   "by_field": {f: 0 for f in want}, "samples": []}

    # ---- 阶段 1: 先筛出真正需要补的曲目(纯查库, 很快) ----
    tasks: list[tuple[dict, list[str], Any]] = []
    for r in rows:
        need = [f for f in want if _is_empty(r, f)]
        if not need:
            stats["skipped"] += 1
            continue
        stats["scanned"] += 1
        if not _has_platform_id(r):
            stats["no_id"] += 1
            continue
        key = f"{r['provider']}:{r['account']}" if r.get("account") else r["provider"]
        provider = ctx.providers.get(key) or ctx.providers.get(r["provider"])
        if provider is None:
            stats["no_source"] += 1
            continue
        tasks.append((r, need, provider))

    # ---- 阶段 2: 并发取详情(瓶颈在网络, 4 路约快 3 倍) ----
    details = _fetch_details(tasks, progress)

    # ---- 阶段 3: 串行写库/写文件(sqlite 与文件写入都不适合并发) ----
    for r, need, _provider in tasks:
        detail = details.get(int(r["id"]))
        if detail is None:
            stats["no_source"] += 1
            continue

        patch: dict[str, Any] = {}
        for f in need:
            v = _detail_value(detail, f)
            if (f == "track_no" and int(v or 0) > 0) or (f != "track_no" and str(v).strip()):
                patch[f] = v
        if not patch:
            stats["no_source"] += 1
            continue

        for f in patch:
            stats["by_field"][f] += 1
        if len(stats["samples"]) < 12:
            stats["samples"].append(
                f"{r['artists']} - {r['title']}: " +
                " / ".join(f"{FIELD_LABEL[f]}={patch[f]}" for f in patch))
        if dry_run:
            stats["updated"] += 1
            continue

        # 1) 更新数据库
        sets = ", ".join(f"{f}=?" for f in patch)
        db.execute(f"UPDATE tracks SET {sets}, updated_at=? WHERE id=?",
                   (*patch.values(), now_iso(), r["id"]))
        stats["updated"] += 1

        # 2) 把新值写回文件标签(播放器才看得见)
        if write_files and r.get("path"):
            f_path = ctx.cfg.music_dir / r["path"]
            if f_path.exists():
                merged = {**r, **patch}
                meta = TrackMeta(
                    title=merged["title"] or "未知歌名",
                    artists=[a.strip() for a in (merged["artists"] or "").split(";") if a.strip()]
                    or ["未知艺术家"],
                    album=merged["album"] or "",
                    albumartist=merged["albumartist"] or "",
                    year=str(merged["year"] or ""),
                    track_no=int(merged["track_no"] or 0),
                    duration=int(merged["duration"] or 0),
                )
                try:
                    org.write_tags(f_path, meta)
                    stats["files_written"] += 1
                except Exception as exc:  # noqa: BLE001 - 标签写失败不算致命
                    stats["failed"] += 1
                    db.log("warn", "scrape", f"写标签失败 {r['path']}: {exc}")

    db.log("info", "scrape",
           f"元数据刮削完成: 更新 {stats['updated']} 条 / 无源 {stats['no_source']} / "
           f"无平台ID {stats['no_id']} / 写文件 {stats['files_written']}"
           + ("（试运行，未写入）" if dry_run else ""))
    return stats
