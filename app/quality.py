"""音质分级: 判断"有损/无损", 以及洗版时比较前后是否真的升级了.

库里 quality 字段存的是**下载时用的档位**(lossless/f000/m800/exhigh/...),
文件名后缀和实际参数另算。这里只做档位到名次的映射, 用于:
  - 曲库统计/库体检 列出"有损"的曲子
  - 洗版时判断"这次下的比原来更好吗", 避免白搬文件
"""
from __future__ import annotations

# 名次越高越好。同档位不同平台叫法对齐到同一名次。
RANK: dict[str, int] = {
    "standard": 1, "m500": 1, "c400": 2,
    "higher": 2,
    "exhigh": 3, "m800": 3,
    "lossless": 4, "f000": 4,
    "hires": 5, "rs01": 5,
    "q000": 6, "ai00": 6, "sb00": 6,
}

# 展示用的中文名
LABEL: dict[str, str] = {
    "standard": "标准", "m500": "128k", "c400": "64k AAC",
    "higher": "较高", "exhigh": "极高", "m800": "320k",
    "lossless": "无损", "f000": "无损", "hires": "Hi-Res", "rs01": "Hi-Res",
    "q000": "母带", "ai00": "全景声", "sb00": "母带 4.0",
}

LOSSLESS_MIN = 4          # 名次 >= 4 视为无损


def rank(quality: str) -> int:
    return RANK.get((quality or "").strip().lower(), 0)


def is_lossless(quality: str) -> bool:
    return rank(quality) >= LOSSLESS_MIN


def is_lossy(quality: str) -> bool:
    """有损(含未知档位). 未知档位当作有损, 交给用户判断."""
    return rank(quality) < LOSSLESS_MIN


def label(quality: str) -> str:
    q = (quality or "").strip().lower()
    return LABEL.get(q, q or "未知")


def better(new: str, old: str) -> bool:
    """新档位是否严格优于旧档位(用于洗版去重)."""
    return rank(new) > rank(old)
