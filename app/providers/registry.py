"""平台注册表: 支持一个平台多个账号.

实例键: "netease"(第一账号的别名, 兼容旧数据) 与 "netease:昵称".
歌单行记录所属账号, 引擎按 (平台, 账号) 找对应会话.
"""
from __future__ import annotations

import os
import re
from dataclasses import replace

from .base import BaseProvider
from .netease import NeteaseProvider
from .qq import QQProvider


def build_providers(cfg, db) -> dict[str, BaseProvider]:
    providers: dict[str, BaseProvider] = {}
    if cfg.netease.enabled:
        _build_platform(providers, "netease", cfg.netease,
                        lambda sub: NeteaseProvider(sub, db))
    if cfg.qq.enabled:
        _build_platform(providers, "qq", cfg.qq,
                        lambda sub: QQProvider(sub, db))
    return providers


def _build_platform(providers: dict, platform: str, platform_cfg, factory) -> None:
    accounts = list(getattr(platform_cfg, "accounts", None) or [])
    if not accounts and platform_cfg.cookie:
        accounts = [("默认", platform_cfg.cookie)]
    for i, (name, cookie) in enumerate(accounts):
        inst = factory(replace(platform_cfg, cookie=cookie))
        inst.account = name
        providers[f"{platform}:{name}"] = inst
        if i == 0:
            providers[platform] = inst    # 第一账号别名


def build_mock_providers(cfg, db) -> dict[str, BaseProvider]:
    from .mock import MockProvider
    out: dict[str, BaseProvider] = {}
    for name, display in (("netease", "网易云"), ("qq", "QQ音乐")):
        p = MockProvider(cfg, db, name=name, display=display)
        p.start()
        out[name] = p
    # 演示/联调用: MUSEHUB_MOCK_SEED="101,102" 预置收藏并自动订阅
    seed = (os.environ.get("MUSEHUB_MOCK_SEED") or "").strip()
    if seed and db is not None:
        ids = [x.strip() for x in seed.split(",") if x.strip()]
        out["netease"].seed_favorites(ids)  # type: ignore[attr-defined]
        row = db.upsert_playlist("netease", "fav", "我喜欢的音乐", "", len(ids))
        db.execute("UPDATE playlists SET subscribed=1 WHERE id=?", (row,))
    return out


def make_providers(cfg, db) -> dict[str, BaseProvider]:
    if os.environ.get("MUSEHUB_MOCK") == "1":
        return build_mock_providers(cfg, db)
    return build_providers(cfg, db)


def resolve_playlist_input(text: str) -> tuple[str, str, str] | None:
    """把用户粘贴的链接/ID 解析成 (provider, remote_id, kind).

    kind: playlist(歌单) / album(专辑) / chart(榜单)
    """
    from .netease import parse_playlist_id as ne_parse
    from .qq import parse_playlist_id as qq_parse

    raw = (text or "").strip()
    if not raw:
        return None
    low = raw.lower()

    if "y.qq.com" in low or "qq.com" in low:
        m = re.search(r"/albumDetail/([A-Za-z0-9]+)", raw) or re.search(r"albumMid=([A-Za-z0-9]+)", raw)
        if m:
            return ("qq", m.group(1), "album")
        m = re.search(r"/toplist/(\d+)", raw) or re.search(r"topId=(\d+)", raw)
        if m:
            return ("qq", m.group(1), "chart")
        pid = qq_parse(raw)
        return ("qq", pid, "playlist") if pid else None

    if "163.com" in low or "music.163" in low:
        m = re.search(r"[?&#]id=(\d+)", raw)
        if "album" in low:
            return ("netease", m.group(1), "album") if m else None
        if "toplist" in low:
            return ("netease", m.group(1), "chart") if m else None
        pid = ne_parse(raw)
        return ("netease", pid, "playlist") if pid else None

    if raw.isdigit():
        return ("netease", raw, "playlist")
    return None
