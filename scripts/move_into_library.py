"""把测试目录里已下载的文件搬进正式音乐库(碰撞安全).

规则:
  * 目标不存在 -> 直接移动
  * 目标已存在(同一首歌老库里就有) -> 保留老库文件, 删掉测试副本, 不产生重复
  * 只动音频文件和同名 .lrc, 不碰 cover.jpg(避免覆盖老库封面)

用法(容器内): python /app/scripts/move_into_library.py [--apply]
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

SRC = Path(os.environ.get("SRC_DIR", "/src"))
DST = Path(os.environ.get("DST_DIR", "/dst"))
APPLY = "--apply" in sys.argv

AUDIO = {".flac", ".mp3", ".m4a", ".ape", ".wav", ".ogg", ".aac", ".wma"}


def main() -> int:
    moved = existed = kept_dup = 0
    errors: list[str] = []
    if not SRC.exists():
        print(f"源目录不存在: {SRC}")
        return 1

    for dirpath, dirnames, filenames in os.walk(SRC):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            suffix = Path(name).suffix.lower()
            if suffix not in AUDIO and suffix != ".lrc":
                continue
            src = Path(dirpath) / name
            rel = src.relative_to(SRC)
            dst = DST / rel
            try:
                if dst.exists():
                    # 老库已有同名文件: 保留老库的, 删掉重复副本
                    if suffix == ".lrc":
                        kept_dup += 1
                        if APPLY:
                            src.unlink()
                        continue
                    same = dst.stat().st_size == src.stat().st_size
                    existed += 1
                    if APPLY:
                        src.unlink()
                        # 对应的 .lrc 也删掉(避免孤儿)
                        lrc = src.with_suffix(".lrc")
                        if lrc.exists():
                            lrc.unlink()
                    print(f"  已存在, 丢弃副本{'（大小一致）' if same else '（大小不同, 保留老库版）'}: {rel}")
                    continue
                if APPLY:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dst))
                moved += 1
            except OSError as exc:
                errors.append(f"{rel}: {exc}")

    print(f"\n{'已执行' if APPLY else '预览(未改动)'}: 移动 {moved} / 目标已存在丢弃 {existed} / lrc 丢弃 {kept_dup}")
    if errors:
        print(f"失败 {len(errors)}:")
        for e in errors[:10]:
            print("  ", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
