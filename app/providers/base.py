"""平台适配层的公共契约.

所有平台实现同一组原语, 上层 sync 引擎只依赖这里的抽象:

    playlist(pid)          -> 歌单元数据 + 曲目列表
    source(track, quality) -> 可下载的直链
    add_tracks / remove_tracks(pid, ids)

关键设计: "收藏" 被建模为 "加入『我喜欢』歌单". 这样收藏/取消收藏与
歌单增删共用同一套原语, 两个平台行为一致, 上层不用为每个平台写特殊分支.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any


class ProviderError(Exception):
    """平台接口错误基类."""


class NotLoggedIn(ProviderError):
    """缺少/失效的登录凭据."""


class NoSource(ProviderError):
    """该曲目在当前账号/音质下没有可用音源."""


class Unsupported(ProviderError):
    """平台不支持该操作."""


@dataclass
class RemoteTrack:
    provider: str
    remote_id: str
    title: str
    artists: list[str] = field(default_factory=list)
    album: str = ""
    album_artist: str = ""
    duration: int = 0          # 秒
    year: str = ""
    track_no: int = 0
    cover_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def artist_str(self) -> str:
        return "; ".join(a for a in self.artists if a) or "未知艺术家"

    @property
    def primary_artist(self) -> str:
        return next((a for a in self.artists if a), "未知艺术家")

    @property
    def effective_album(self) -> str:
        """专辑名缺失或与歌名相同时, 视为单曲."""
        al = (self.album or "").strip()
        if not al:
            return ""
        if al.casefold() == (self.title or "").strip().casefold():
            return ""
        return al

    def to_row(self) -> dict:
        return {
            "provider": self.provider,
            "remote_id": str(self.remote_id),
            "title": self.title,
            "artists": self.artist_str,
            "album": self.effective_album,
            "albumartist": self.album_artist or self.primary_artist,
            "duration": self.duration,
            "year": self.year,
            "track_no": self.track_no,
            "cover": self.cover_url,
        }

    def search_key(self) -> tuple[str, str]:
        def norm(s: str) -> str:
            import re
            return re.sub(r"[\s\u3000\W_]+", "", (s or "").casefold())
        return norm(self.title), norm(self.primary_artist)


@dataclass
class DownloadSource:
    url: str
    ext: str = "mp3"
    quality: str = ""
    bitrate: int = 0
    size: int = 0
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class RemotePlaylist:
    provider: str
    remote_id: str
    name: str
    cover: str = ""
    tracks: list[RemoteTrack] = field(default_factory=list)
    owner: str = ""
    is_favorite: bool = False
    # playlist(歌单) / album(专辑) / chart(榜单)
    kind: str = "playlist"
    # 平台自己声明的曲目总数; -1 表示未知. 用来区分"真的空了"和"接口抽风"
    declared_count: int = -1


class BaseProvider:
    name: str = "base"
    display: str = "Base"

    def __init__(self, cfg: Any, db: Any = None):
        self.cfg = cfg
        self.db = db

    # --- 元信息 ---
    def logged_in(self) -> bool:
        raise NotImplementedError

    def reload(self) -> None:
        """配置(通常是 Cookie)变更后重新加载凭据. 默认无操作."""
        return None

    def vip_status(self, refresh: bool = False) -> str:
        """会员状态(带 1 小时缓存). 子类实现 _probe_vip; 不支持的返回空."""
        if not hasattr(self, "_probe_vip") or self.db is None:
            return ""
        key = f"vip:{self.name}"
        if not refresh:
            raw = self.db.kv_get(key, "")
            if raw:
                try:
                    d = json.loads(raw)
                    if time.time() - float(d.get("ts", 0)) < 3600:
                        return str(d.get("status", ""))
                except Exception:  # noqa: BLE001
                    pass
        try:
            status = self._probe_vip()
        except Exception as exc:  # noqa: BLE001
            status = f"检测失败: {str(exc)[:80]}"
        self.db.kv_set(key, json.dumps({"ts": time.time(), "status": status},
                                       ensure_ascii=False))
        return status

    def status(self) -> str:
        return "ok" if self.logged_in() else "未登录"

    # --- 歌单 ---
    def my_playlists(self) -> list[RemotePlaylist]:
        """返回当前账号的歌单(不含曲目)."""
        raise NotImplementedError

    def playlist(self, remote_id: str) -> RemotePlaylist:
        raise NotImplementedError

    def favorite_playlist_id(self) -> str | None:
        """『我喜欢』歌单 id, 用于把收藏建模成歌单成员关系."""
        return None

    # --- 专辑 / 榜单订阅(与歌单同构, 都归一成 RemotePlaylist) ---
    def album(self, remote_id: str) -> RemotePlaylist:
        raise Unsupported(f"{self.display} 不支持专辑订阅")

    def chart(self, remote_id: str) -> RemotePlaylist:
        raise Unsupported(f"{self.display} 不支持榜单订阅")

    def auth_check(self) -> tuple[bool, str]:
        """登录态(Cookie)是否仍然有效. 返回 (是否有效, 说明).

        默认不检查(视为有效); 具体平台自己实现, 用于 Cookie 过期提醒。
        """
        return True, ""

    # --- 元数据刮削 ---
    def track_detail(self, remote_id: str) -> "RemoteTrack | None":
        """按平台 ID 取一首歌的权威元数据(用于补年份/音轨号/专辑/封面).

        与 search 不同, 这里拿到的是"就是这首歌", 不存在匹配错版本的问题。
        取不到返回 None。
        """
        return None

    # --- 搜索/音源 ---
    def search(self, keyword: str, limit: int = 20) -> list[RemoteTrack]:
        raise NotImplementedError

    def source(self, track: RemoteTrack, quality: str) -> DownloadSource:
        raise NotImplementedError

    def lyric(self, track: RemoteTrack) -> str:
        return ""

    # --- 写操作 ---
    def add_tracks(self, remote_id: str, track_ids: list[str]) -> None:
        raise NotImplementedError

    def remove_tracks(self, remote_id: str, track_ids: list[str]) -> None:
        raise NotImplementedError

    def favorite(self, track: RemoteTrack, on: bool = True) -> None:
        pid = self.favorite_playlist_id()
        if not pid:
            raise Unsupported(f"{self.display} 无法定位『我喜欢』歌单")
        if on:
            self.add_tracks(pid, [str(track.remote_id)])
        else:
            self.remove_tracks(pid, [str(track.remote_id)])
