"""把已有音乐库导入 MuseHub 数据库(只读扫描, 不移动/不修改任何文件).

目的:
  1. 让曲库统计包含原有文件;
  2. 让"下载前去重"能看到老库里的同一首歌 —— 已有就不再重复下载;
  3. 这些记录一律 managed=0, 任何删除逻辑都不会碰它们。

用法:
  python -m app import            # 只统计
  python -m app import --apply    # 写入数据库
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .db import Database, now_iso
from .organizer import AUDIO_EXT

SKIP_DIRS = {"#recycle", "@eadir", ".@__thumb", "$recycle.bin", "system volume information"}


def _norm(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", (s or "").casefold())


def read_tags(path: Path) -> dict | None:
    try:
        from mutagen import File as MFile
        audio = MFile(str(path))
        if audio is None:
            return None
        tags = audio.tags or {}
        def get(*keys: str) -> str:
            for k in keys:
                try:
                    val = tags.get(k)
                except ValueError:
                    continue      # mutagen 对空标签值会抛 ValueError
                if val:
                    return str(val[0]) if isinstance(val, (list, tuple)) else str(val)
            return ""
        title = get("title", "TIT2") or path.stem.split(" - ")[-1]
        artist = get("artist", "TPE1") or (path.stem.split(" - ")[0] if " - " in path.stem else "")
        album = get("album", "TALB")
        albumartist = get("albumartist", "TPE2") or artist.split(";")[0].strip()
        year = (get("date", "TDRC", "©day") or "")[:4]
        length = int(getattr(getattr(audio, "info", None), "length", 0) or 0)
        return {"title": title, "artists": artist, "album": album,
                "albumartist": albumartist, "year": year, "duration": length}
    except Exception:  # noqa: BLE001
        return None


def scan(root: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if Path(name).suffix.lower() in AUDIO_EXT:
                out.append(Path(dirpath) / name)
    return out


def run(music_dir: Path, db: Database, apply: bool = False, limit: int = 0) -> dict:
    files = scan(music_dir)
    if limit:
        files = files[:limit]
    known = {r["path"] for r in db.query(
        "SELECT path FROM tracks WHERE status='active' AND path<>''")}
    added = skipped = failed = 0
    ts = now_iso()
    for f in files:
        rel = f.relative_to(music_dir).as_posix()
        if rel in known:
            skipped += 1
            continue
        tags = read_tags(f)
        if not tags:
            failed += 1
            continue
        if not apply:
            added += 1
            continue
        _score, quality = audio_score(f)
        db.upsert_track({
            "provider": "local", "remote_id": rel,
            "title": tags["title"], "artists": tags["artists"],
            "album": tags["album"], "albumartist": tags["albumartist"],
            "year": tags["year"], "duration": tags["duration"],
            "ext": f.suffix.lstrip("."), "path": rel,
            "size": f.stat().st_size, "managed": 0, "status": "active",
            "quality": quality or "local",
        })
        added += 1
    return {"files": len(files), "added": added, "skipped": skipped, "failed": failed,
            "applied": apply}


# ---------- 音质评估 ----------
LOSSLESS_SUFFIX = {".flac", ".ape", ".wav", ".aiff", ".wv"}


def audio_score(path: Path) -> tuple[int, str]:
    """按文件**真实参数**评估音质. 返回 (评分, 品质标签).

    无损永远优先于有损; 同类里位深/采样率/码率越高越好。
    评分只用于同曲多版本之间比大小, 数值本身没有绝对含义。
    """
    try:
        from mutagen import File as MFile
        a = MFile(str(path))
        if a is None:
            return -1, ""
        info = getattr(a, "info", None)
        sr = int(getattr(info, "sample_rate", 0) or 0)
        bits = int(getattr(info, "bits_per_sample", 0) or 0)
        br = int(getattr(info, "bitrate", 0) or 0)
        suffix = path.suffix.lower()
        if suffix in LOSSLESS_SUFFIX:
            q = "hires" if (bits >= 24 or sr > 48000) else "lossless"
            return 1_000_000 + bits * 10_000 + sr, q
        if suffix == ".m4a":
            q = "exhigh" if br >= 256_000 else "standard"
            return br, q
        # mp3 等有损格式
        q = "exhigh" if br >= 300_000 else ("higher" if br >= 180_000 else "standard")
        return br, q
    except Exception:  # noqa: BLE001 - 坏文件不能拖垮整个流程
        return -1, ""
