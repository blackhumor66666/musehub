"""Mock 平台, 仅用于离线自测.

起一个本地 HTTP 服务真的吐音频字节, 这样下载/校验/归档整条链路都和
生产一致, 不会出现"测试通过、上线挂掉"的情况.
"""
from __future__ import annotations

import base64
import io
import math
import struct
import threading
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .base import BaseProvider, DownloadSource, ProviderError, RemotePlaylist, RemoteTrack

SAMPLE_RATE = 8000
DURATION = 10


def _make_cover_png(size: int = 64) -> bytes:
    """生成一张真实尺寸的 PNG(约 12KB).

    为什么不用 1x1 的占位图: Downloader.fetch_image 有 "大于 1KB 才算真图" 的保护,
    几十字节的占位图会被当成坏图丢掉, 测试里就永远测不到封面链路。
    """
    import struct
    import zlib

    raw = bytearray()
    for y in range(size):
        raw.append(0)                      # 每行的 filter 字节
        for x in range(size):
            raw += bytes(((x * 4) % 256, (y * 4) % 256, 180))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 0)) + chunk(b"IEND", b""))


COVER_PNG = _make_cover_png()


def _make_wav(freq: float) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        frames = bytearray()
        for i in range(SAMPLE_RATE * DURATION):
            val = int(12000 * math.sin(2 * math.pi * freq * i / SAMPLE_RATE))
            frames += struct.pack("<h", val)
        wf.writeframes(bytes(frames))
    return buf.getvalue()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        audio = self.server.audio  # type: ignore[attr-defined]
        if self.path.startswith("/audio/"):
            key = self.path.rsplit("/", 1)[-1].split(".")[0]
            data = audio.get(key)
            if data:
                self.send_response(200)
                self.send_header("Content-Type", "audio/x-wav")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        if self.path.startswith("/cover"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(COVER_PNG)))
            self.end_headers()
            self.wfile.write(COVER_PNG)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


class MockProvider(BaseProvider):
    """两个平台共用一份实现, name 由构造参数决定."""

    def __init__(self, cfg, db=None, name: str = "netease", display: str = "Mock"):
        super().__init__(cfg, db)
        self.name = name
        self.display = display
        self._lock = threading.Lock()
        self._favorites: set[str] = set()
        self._playlists: dict[str, list[str]] = {"fav": []}
        self._chart_members: dict[str, list[str]] = {}
        self._server: ThreadingHTTPServer | None = None
        self._port = 0
        self._catalog = self._build_catalog()
        self._audio = {t.remote_id: _make_wav(220 + i * 40)
                       for i, t in enumerate(self._catalog)}
        self.start()

    # ---------- 服务 ----------
    def start(self) -> None:
        if self._server:
            return
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.audio = self._audio  # type: ignore[attr-defined]
        self._port = server.server_address[1]
        self._server = server
        threading.Thread(target=server.serve_forever, daemon=True).start()
        for t in self._catalog:
            t.cover_url = f"http://127.0.0.1:{self._port}/cover.png"

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # ---------- 目录 ----------
    def _build_catalog(self) -> list[RemoteTrack]:
        raw = [
            ("101", "晴天", ["周杰伦"], "叶惠美", 2003, 1),
            ("102", "Titanium", ["David Guetta", "Sia"], "Nothing But The Beat", 2011, 2),
            ("103", "Almost Lover", ["A Fine Frenzy"], "One Cell In The Sea", 2007, 3),
            ("104", "孤勇者", ["陈奕迅"], "孤勇者", 2021, 4),      # 单曲: 专辑名=歌名
            ("105", "Dream It Possible", ["Delacey"], "Dream It Possible", 2015, 5),
            ("106", "海阔天空", ["Beyond"], "乐与怒", 1993, 6),
        ]
        out = []
        for rid, title, artists, album, year, no in raw:
            out.append(RemoteTrack(
                provider=self.name, remote_id=rid, title=title, artists=artists,
                album=album, album_artist=artists[0], duration=DURATION,
                year=str(year), track_no=no,
                cover_url=f"http://127.0.0.1:{self._port}/cover.jpg" if self._port else "",
            ))
        return out

    # ---------- 契约 ----------
    def logged_in(self) -> bool:
        return True

    def status(self) -> str:
        return "mock"

    def my_playlists(self) -> list[RemotePlaylist]:
        return [RemotePlaylist(provider=self.name, remote_id="fav", name="我喜欢的音乐",
                               is_favorite=True)]

    def favorite_playlist_id(self) -> str:
        return "fav"

    def playlist(self, remote_id: str) -> RemotePlaylist:
        with self._lock:
            if remote_id == "fav":
                ids = list(self._favorites)
            else:
                ids = list(self._playlists.get(remote_id, []))
        by_id = {t.remote_id: t for t in self._catalog}
        tracks = [by_id[i] for i in ids if i in by_id]
        return RemotePlaylist(provider=self.name, remote_id=remote_id,
                              name="我喜欢的音乐" if remote_id == "fav" else remote_id,
                              tracks=tracks, is_favorite=remote_id == "fav",
                              declared_count=len(tracks))

    def track_detail(self, remote_id: str) -> RemoteTrack | None:
        for t in self._catalog:
            if t.remote_id == str(remote_id):
                return t
        return None

    def album(self, remote_id: str) -> RemotePlaylist:
        """专辑订阅: 用专辑名匹配目录里的歌, 模拟"一张专辑若干首"."""
        if remote_id == "al_hairui":
            tracks = [t for t in self._catalog if t.album == "叶惠美"]
            name = "叶惠美"
        else:
            tracks = [t for t in self._catalog if t.album == "乐与怒"]
            name = "乐与怒"
        if not tracks:
            raise ProviderError(f"mock 专辑 {remote_id} 没有歌曲")
        return RemotePlaylist(provider=self.name, remote_id=remote_id, name=name,
                              tracks=tracks, kind="album", declared_count=len(tracks))

    def chart(self, remote_id: str) -> RemotePlaylist:
        """榜单订阅: 默认取目录前两首, 测试可通过 _chart_members 改内容模拟换榜."""
        ids = self._chart_members.get(remote_id) or [t.remote_id for t in self._catalog[:2]]
        by_id = {t.remote_id: t for t in self._catalog}
        tracks = [by_id[i] for i in ids if i in by_id]
        return RemotePlaylist(provider=self.name, remote_id=remote_id,
                              name=f"mock榜单 {remote_id}", tracks=tracks, kind="chart",
                              declared_count=len(tracks))

    def search(self, keyword: str, limit: int = 20) -> list[RemoteTrack]:
        # 按词匹配(任一词命中即可), 与真实平台"歌手 歌名"的搜索行为一致
        words = [w for w in keyword.casefold().split() if w]
        hits = [t for t in self._catalog
                if any(w in t.title.casefold() or w in t.artist_str.casefold()
                       for w in words)]
        return hits[:limit]

    def source(self, track: RemoteTrack, quality: str) -> DownloadSource:
        return DownloadSource(
            url=f"http://127.0.0.1:{self._port}/audio/{track.remote_id}.wav",
            ext="wav", quality=quality or "mock",
        )

    def add_tracks(self, remote_id: str, track_ids: list[str]) -> None:
        with self._lock:
            if remote_id == "fav":
                self._favorites.update(str(x) for x in track_ids)
            else:
                self._playlists.setdefault(remote_id, []).extend(str(x) for x in track_ids)

    def remove_tracks(self, remote_id: str, track_ids: list[str]) -> None:
        with self._lock:
            if remote_id == "fav":
                for x in track_ids:
                    self._favorites.discard(str(x))
            else:
                cur = self._playlists.get(remote_id, [])
                self._playlists[remote_id] = [x for x in cur if x not in {str(i) for i in track_ids}]

    # ---------- 测试辅助 ----------
    def seed_favorites(self, ids: list[str]) -> None:
        with self._lock:
            self._favorites = set(ids)

    def favorites(self) -> set[str]:
        with self._lock:
            return set(self._favorites)
