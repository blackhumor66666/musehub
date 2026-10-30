"""库内体检与批量修复: 缺封面 / 缺歌词.

为什么要单独做:
  - 早期下载的歌、用户自己放进来的歌, 常常没有封面或 .lrc;
  - 播放器里表现为"一片灰色 + 没歌词", 但看不出到底缺哪个、缺多少;
  - 所以先扫一遍给出清单(哪些歌缺什么), 再支持一键补齐。

判定口径(按播放器实际能看到的效果):
  - 封面: 文件内嵌图片(FLAC pictures / ID3 APIC / MP4 covr) 或 同目录有 cover.jpg 等
  - 歌词: 同名 .lrc 存在 或 文件内嵌歌词(USLT/LYRICS)
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterable

from mutagen import File as MutagenFile

from .db import now_iso
from .organizer import Organizer
from .providers.base import RemoteTrack

COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg", "folder.png",
               "album.jpg", "albumart.jpg")


def _open(path: Path):
    try:
        return MutagenFile(str(path))
    except Exception:  # noqa: BLE001 - 坏文件/不支持的格式都当成"读不到标签"
        return None


def has_cover(audio: Any, path: Path) -> bool:
    if audio is not None:
        try:
            if getattr(audio, "pictures", None):
                return True
            tags = getattr(audio, "tags", None)
            if tags:
                for k in tags.keys():
                    kk = str(k)
                    if kk.startswith("APIC") or kk.lower() in (
                            "covr", "metadata_block_picture"):
                        return True
        except Exception:  # noqa: BLE001
            pass
    parent = path.parent
    return any((parent / n).exists() for n in COVER_NAMES)


def has_lyric(audio: Any, path: Path) -> bool:
    if path.with_suffix(".lrc").exists():
        return True
    try:
        tags = getattr(audio, "tags", None)
        if tags:
            for k in tags.keys():
                kk = str(k).lower()
                if kk.startswith("uslt") or kk in ("lyrics", "unsyncedlyrics", "syncedlyrics"):
                    return True
    except Exception:  # noqa: BLE001
        pass
    return False


def scan(db: Any, cfg: Any) -> dict:
    """扫描全库, 返回缺失清单. 只读, 不改任何文件."""
    started = time.time()
    rows = [dict(r) for r in db.query(
        """SELECT id, provider, remote_id, title, artists, album, path, cover
           FROM tracks WHERE status='active' AND path<>'' ORDER BY id""")]
    items: list[dict] = []
    checked = miss_cover = miss_lyric = gone = 0
    ign = db.ignored_ids()
    ign_cover = ign.get("cover", set())
    ign_lyric = ign.get("lyric", set())
    for r in rows:
        f = cfg.music_dir / r["path"]
        if not f.exists():
            gone += 1
            continue
        checked += 1
        audio = _open(f)
        missing: list[str] = []
        tid = int(r["id"])
        # 用户点过"忽略"的不再计入缺失, 否则每次检测都刷同一批
        if tid not in ign_cover and not has_cover(audio, f):
            missing.append("cover")
            miss_cover += 1
        if tid not in ign_lyric and not has_lyric(audio, f):
            missing.append("lyric")
            miss_lyric += 1
        if missing:
            items.append({
                "id": int(r["id"]), "provider": r["provider"],
                "title": r["title"], "artists": r["artists"], "album": r["album"],
                "path": r["path"], "missing": missing,
            })
    return {
        "checked": checked,
        "file_missing": gone,
        "missing_cover": miss_cover,
        "missing_lyric": miss_lyric,
        "both": sum(1 for i in items if len(i["missing"]) == 2),
        "items": items,
        "elapsed": round(time.time() - started, 1),
        "scanned_at": now_iso(),
    }


def _track_of(row: dict) -> RemoteTrack:
    return RemoteTrack(
        provider=row["provider"], remote_id=str(row["remote_id"]),
        title=row["title"],
        artists=[a.strip() for a in (row["artists"] or "").split(";") if a.strip()],
        album=row["album"] or "",
        cover_url=row.get("cover") or "",
    )


def _cover_url(ctx: Any, row: dict) -> str:
    # 只用入库时记录的封面地址, 不做搜索兜底 —— 现场版/翻唱搜索出来的封面
    # 大概率是别的歌的图, 挂错比缺图更糟。
    return row.get("cover") or ""


def fix(ctx: Any, ids: Iterable[int] | None = None,
        kinds: Iterable[str] = ("cover", "lyric"),
        progress: dict | None = None) -> dict:
    """按清单补封面/歌词. ids 为空表示"全库所有缺的". """
    db, cfg = ctx.db, ctx.cfg
    org = Organizer(cfg.organizer)
    want = set(kinds or ("cover", "lyric"))
    rows = [dict(r) for r in db.query(
        """SELECT id, provider, remote_id, title, artists, album, path, cover
           FROM tracks WHERE status='active' AND path<>'' ORDER BY id""")]
    if ids is not None:
        wanted = {int(i) for i in ids}
        rows = [r for r in rows if int(r["id"]) in wanted]

    out = {"fixed_cover": 0, "fixed_lyric": 0, "no_source": 0,
           "no_source_names": [], "skipped": 0, "failed": 0, "total": len(rows)}
    _ign = db.ignored_ids()
    ign_cover, ign_lyric = _ign.get("cover", set()), _ign.get("lyric", set())
    for idx, r in enumerate(rows, 1):
        label = f"{r['artists']} - {r['title']}"
        f = cfg.music_dir / r["path"]
        if not f.exists():
            out["skipped"] += 1
            continue
        audio = _open(f)
        tid = int(r["id"])
        need_cover = ("cover" in want and tid not in ign_cover
                      and not has_cover(audio, f))
        need_lyric = ("lyric" in want and tid not in ign_lyric
                      and not has_lyric(audio, f))
        if not need_cover and not need_lyric:
            out["skipped"] += 1
            continue
        if progress is not None:
            progress.update({"done": idx, "total": len(rows),
                             "current": f"{r['artists']} - {r['title']}"})

        if need_cover:
            url = _cover_url(ctx, r)
            img = ctx.downloader.fetch_image(url) if url else None
            if img:
                try:
                    org.embed_cover(f, img)
                    org.save_cover_file(f, img)
                    out["fixed_cover"] += 1
                except Exception as exc:  # noqa: BLE001
                    out["failed"] += 1
                    db.log("warn", "repair", f"写封面失败 {r['title']}: {exc}")
            else:
                out["no_source"] += 1
                out["no_source_names"].append(f"{label}（封面）")

        if need_lyric:
            provider = ctx.providers.get(r["provider"])
            lyric = ""
            if provider is not None:
                try:
                    lyric = provider.lyric(_track_of(r))
                except Exception:  # noqa: BLE001 - 单曲取词失败不影响其他
                    lyric = ""
            if lyric:
                try:
                    org.save_lyric(f, lyric)
                    out["fixed_lyric"] += 1
                except Exception as exc:  # noqa: BLE001
                    out["failed"] += 1
                    db.log("warn", "repair", f"写歌词失败 {r['title']}: {exc}")
            else:
                out["no_source"] += 1
                out["no_source_names"].append(f"{label}（歌词）")
    db.log("info", "repair",
           f"批量修复完成: 封面 +{out['fixed_cover']} / 歌词 +{out['fixed_lyric']} / "
           f"无源 {out['no_source']} / 失败 {out['failed']}")
    return out
