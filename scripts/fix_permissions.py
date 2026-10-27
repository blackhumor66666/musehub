"""修正音乐库里权限过窄的文件/目录.

背景: mutagen 保存标签时会**重建**文件, 新文件是 700 root:root。
音乐库是 SMB 共享给 Windows 的, 非 root 用户就读不到、改不了。

做法: 只动"其他用户没有读写权限"的项(700/600 等), 已经是 777/666 的不碰,
所以对用户原有文件零影响。

用法:
  python scripts/fix_permissions.py [音乐库路径, 默认 /music]
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

FILE_MODE = 0o666
DIR_MODE = 0o777


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "/music")
    if not root.exists():
        print(f"路径不存在: {root}")
        return 1
    files = dirs = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        try:
            if (d.stat().st_mode & 0o007) != 0o007:
                os.chmod(d, DIR_MODE)
                dirs += 1
        except OSError:
            pass
        for name in filenames:
            f = d / name
            try:
                if (f.stat().st_mode & 0o006) != 0o006:
                    os.chmod(f, FILE_MODE)
                    files += 1
            except OSError:
                pass
    print(f"修正完成: 文件 {files} 个, 目录 {dirs} 个（已是 777/666 的未改动）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
