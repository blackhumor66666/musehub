"""网易云音乐适配.

接口形态: 官方 Web 端使用的 /weapi/* 接口, 请求体需要两层 AES + 一次 RSA.
不需要第三方 API 服务, 直接带自己的 MUSIC_U cookie 说话.

注意: 未登录也能搜索和读公开歌单, 但下载链接只会返回 128k 试听片段;
要下无损必须配置 NETEASE_COOKIE.
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import string
import time
from typing import Any

import requests
from Crypto.Cipher import AES

from .base import (
    BaseProvider,
    DownloadSource,
    NoSource,
    NotLoggedIn,
    ProviderError,
    RemotePlaylist,
    RemoteTrack,
)

MODULUS = (
    "00e0b509f6259df8642dbc35662901477df22677ec152b5ff68ace615bb7b725152b3ab17a876aea8a5aa76d2e417629"
    "ec4ee341f56135fccf695280104e0312ecbda92557c93870114af6c9d05c4f7f0c3685b7a46bee255932575cce10b42"
    "4d813cfe4875d3e82047b97ddef52741d546b8e289dc6935b3ece0462db0a22b8e7"
)
PUBKEY = "010001"
NONCE = "0CoJUm6Qyw8W8jud"
IV = "0102030405060708"
BASE = "https://music.163.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 音质代号 -> 文件后缀
LEVEL_EXT = {
    "standard": "mp3",
    "higher": "mp3",
    "exhigh": "mp3",
    "lossless": "flac",
    "hires": "flac",
    "jyeffect": "flac",
    "sky": "flac",
    "jymaster": "flac",
}


def _pkcs7(text: bytes) -> bytes:
    pad = 16 - len(text) % 16
    return text + bytes([pad]) * pad


def _aes_b64(text: str, key: str) -> str:
    cipher = AES.new(key.encode(), AES.MODE_CBC, IV.encode())
    return base64.b64encode(cipher.encrypt(_pkcs7(text.encode()))).decode()


def _rsa_encrypt(secret: str) -> str:
    reversed_secret = secret[::-1]
    rs = int(reversed_secret.encode().hex(), 16)
    return format(pow(rs, int(PUBKEY, 16), int(MODULUS, 16)), "x").zfill(256)


def weapi(payload: dict) -> dict[str, str]:
    """网易云 weapi 加密."""
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    secret = "".join(random.choices(string.ascii_letters + string.digits, k=16))
    params = _aes_b64(_aes_b64(text, NONCE), secret)
    return {"params": params, "encSecKey": _rsa_encrypt(secret)}


def _parse_cookie(cookie: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (cookie or "").split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out


class NeteaseProvider(BaseProvider):
    name = "netease"
    display = "网易云"

    def __init__(self, cfg, db=None):
        super().__init__(cfg, db)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Referer": BASE,
            "Origin": BASE,
            "Content-Type": "application/x-www-form-urlencoded",
        })
        cookie = getattr(cfg, "cookie", "") or ""
        if cookie:
            self.session.headers["Cookie"] = cookie
        self.cookies = _parse_cookie(cookie)
        self._uid: str | None = None
        self._fav_id: str | None = None
        self._fav_checked = False

    # ---------- 基础 ----------
    @property
    def csrf(self) -> str:
        return self.cookies.get("__csrf", "")

    def _post(self, path: str, payload: dict, timeout: int = 20) -> dict:
        body = dict(payload)
        body.setdefault("csrf_token", self.csrf)
        try:
            resp = self.session.post(
                BASE + path, data=weapi(body), timeout=timeout,
                headers={"Referer": BASE + "/"},
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise ProviderError(f"网易云请求失败 {path}: {exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"网易云返回非 JSON {path}: {exc}") from exc

    def logged_in(self) -> bool:
        return bool(self.cookies.get("MUSIC_U"))

    def reload(self) -> None:
        """面板改了 Cookie 后热加载, 不重启容器."""
        cookie = getattr(self.cfg, "cookie", "") or ""
        if cookie:
            self.session.headers["Cookie"] = cookie
        else:
            self.session.headers.pop("Cookie", None)
        self.cookies = _parse_cookie(cookie)
        self._uid = None
        self._fav_id = None
        self._fav_checked = False

    def status(self) -> str:
        if not self.logged_in():
            return "未登录(仅搜索/公开歌单)"
        try:
            uid = self.user_id()
            return f"已登录 uid={uid}" if uid else "Cookie 已配置但无法获取 uid"
        except ProviderError as exc:
            return f"Cookie 异常: {exc}"

    def user_id(self) -> str:
        if self._uid:
            return self._uid
        data = self._post("/weapi/w/nuser/account/get", {})
        profile = (data.get("profile") or {}) if isinstance(data, dict) else {}
        uid = str(profile.get("userId") or "")
        if not uid:
            # 部分账号接口只返回 account
            acc = (data.get("account") or {}) if isinstance(data, dict) else {}
            uid = str(acc.get("id") or "")
        if not uid:
            raise NotLoggedIn("无法解析网易云 uid, Cookie 可能已过期")
        self._uid = uid
        return uid

    def _probe_vip(self) -> str:
        data = self._post("/weapi/w/nuser/account/get", {})
        profile = (data.get("profile") or {}) if isinstance(data, dict) else {}
        vt = int(profile.get("vipType") or 0)
        if vt == 0:
            return "无 VIP（VIP 曲目只能试听）"
        known = {10: "VIP（月）", 11: "黑胶 VIP", 12: "年费黑胶 VIP",
                 20: "普通 SVIP", 21: "超级黑胶 SVIP", 100: "黑胶 SVIP", 110: "黑胶 SVIP"}
        return known.get(vt, f"已开通会员 (vipType={vt})")

    # ---------- 歌单 ----------
    def my_playlists(self) -> list[RemotePlaylist]:
        uid = self.user_id()
        data = self._post("/weapi/user/playlist", {"uid": uid, "limit": 1000, "offset": 0, "includeVideo": True})
        out: list[RemotePlaylist] = []
        for pl in (data.get("playlist") or []):
            out.append(RemotePlaylist(
                provider=self.name,
                remote_id=str(pl.get("id")),
                name=pl.get("name") or "",
                cover=pl.get("coverImgUrl") or "",
                owner=(pl.get("creator") or {}).get("nickname", ""),
                is_favorite=bool(pl.get("specialType") == 5),
            ))
        return out

    def favorite_playlist_id(self) -> str | None:
        if self._fav_checked:
            return self._fav_id
        self._fav_checked = True
        try:
            for pl in self.my_playlists():
                if pl.is_favorite:
                    self._fav_id = pl.remote_id
                    return self._fav_id
            # 兜底: 名字匹配
            for pl in self.my_playlists():
                if pl.name in ("我喜欢的音乐", "喜欢的音乐"):
                    self._fav_id = pl.remote_id
                    return self._fav_id
        except ProviderError:
            pass
        return None

    def playlist(self, remote_id: str) -> RemotePlaylist:
        data = self._post("/weapi/v6/playlist/detail", {"id": str(remote_id), "n": 1000, "s": 8})
        pl = data.get("playlist") or {}
        if not pl:
            raise ProviderError(f"网易云歌单 {remote_id} 不存在或不可见")
        raw_tracks = pl.get("tracks") or []
        track_ids = [str(t.get("id")) for t in (pl.get("trackIds") or [])]

        tracks = [self._to_track(t) for t in raw_tracks if t.get("id")]
        have = {t.remote_id for t in tracks}
        missing = [tid for tid in track_ids if tid not in have]
        if missing:
            tracks.extend(self._song_details(missing))
        order = {tid: i for i, tid in enumerate(track_ids)}
        tracks.sort(key=lambda t: order.get(t.remote_id, 10**9))
        return RemotePlaylist(
            provider=self.name,
            remote_id=str(pl.get("id") or remote_id),
            name=pl.get("name") or "",
            cover=pl.get("coverImgUrl") or "",
            tracks=tracks,
            owner=(pl.get("creator") or {}).get("nickname", ""),
            is_favorite=bool(pl.get("specialType") == 5),
            declared_count=int(pl.get("trackCount") or -1),
        )

    def track_detail(self, remote_id: str) -> RemoteTrack | None:
        """按歌曲 ID 取权威元数据(补年份/音轨号/专辑/封面)."""
        rid = str(remote_id)
        if not rid.isdigit():
            return None
        try:
            data = self._post("/weapi/v3/song/detail", {"c": json.dumps([{"id": int(rid)}])})
        except ProviderError:
            return None
        songs = data.get("songs") or []
        return self._to_track(songs[0]) if songs else None

    def album(self, remote_id: str) -> RemotePlaylist:
        """专辑订阅: 把一张专辑当成一个只读歌单."""
        data = self._post(f"/weapi/v1/album/{remote_id}", {"csrf_token": ""})
        if data.get("code") not in (None, 200):
            raise ProviderError(f"网易云专辑 {remote_id} 拉取失败: code={data.get('code')}")
        album = data.get("album") or {}
        songs = [s for s in (data.get("songs") or []) if s.get("id")]
        if not songs:
            raise ProviderError(f"网易云专辑 {remote_id} 里没有歌曲(或 id 不对)")
        tracks = [self._to_track(s) for s in songs]
        return RemotePlaylist(
            provider=self.name,
            remote_id=str(remote_id),
            name=(album.get("name") or f"专辑 {remote_id}").strip(),
            cover=album.get("picUrl") or "",
            tracks=tracks,
            owner=((album.get("artist") or {}).get("name") or ""),
            kind="album",
            declared_count=len(tracks),
        )

    def chart(self, remote_id: str) -> RemotePlaylist:
        """榜单订阅: 网易云的榜单本身就是特殊歌单, 复用歌单接口."""
        pl = self.playlist(remote_id)
        pl.kind = "chart"
        return pl

    def auth_check(self) -> tuple[bool, str]:
        """Cookie 是否还有效(账号信息接口需要登录态)."""
        try:
            data = self._post("/weapi/w/nuser/account/get", {"csrf_token": ""})
        except ProviderError as exc:
            return False, f"接口不可达: {str(exc)[:120]}"
        if data.get("code") not in (None, 200):
            return False, f"接口返回 code={data.get('code')}"
        if not data.get("profile"):
            return False, "Cookie 已失效（未登录），请重新粘贴网易云 Cookie"
        return True, ""

    def _song_details(self, ids: list[str]) -> list[RemoteTrack]:
        out: list[RemoteTrack] = []
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            c = json.dumps([{"id": int(x)} for x in chunk])
            try:
                data = self._post("/weapi/v3/song/detail", {"c": c})
            except ProviderError:
                continue
            for song in (data.get("songs") or []):
                out.append(self._to_track(song))
        return out

    def _to_track(self, s: dict) -> RemoteTrack:
        album = s.get("al") or s.get("album") or {}
        artists = [a.get("name", "") for a in (s.get("ar") or s.get("artists") or []) if a.get("name")]
        year = ""
        pt = album.get("publishTime")
        if pt:
            year = time.strftime("%Y", time.localtime(pt / 1000))
        return RemoteTrack(
            provider=self.name,
            remote_id=str(s.get("id")),
            title=(s.get("name") or "").strip(),
            artists=artists,
            album=(album.get("name") or "").strip(),
            album_artist=artists[0] if artists else "",
            duration=int((s.get("dt") or s.get("duration") or 0) // 1000),
            year=year,
            track_no=int(s.get("no") or 0),
            cover_url=album.get("picUrl") or album.get("blurPicUrl") or s.get("picUrl") or "",
            raw={"fee": s.get("fee"), "mv": s.get("mv"), "album_id": album.get("id")},
        )

    # ---------- 搜索 ----------
    def search(self, keyword: str, limit: int = 20) -> list[RemoteTrack]:
        # cloudsearch 在匿名态会返回 50000005, 所以先用老接口, 失败再试新的
        for path, body in (
            ("/weapi/search/get", {"s": keyword, "type": 1, "limit": limit, "offset": 0}),
            ("/weapi/cloudsearch/get/web",
             {"s": keyword, "type": 1, "limit": limit, "offset": 0, "total": True}),
        ):
            try:
                data = self._post(path, body)
            except ProviderError:
                continue
            if data.get("code") not in (None, 200):
                continue
            result = data.get("result") or {}
            songs = result.get("songs") or []
            if songs:
                return [self._to_track(s) for s in songs]
        return []

    # ---------- 音源 ----------
    def source(self, track: RemoteTrack, quality: str) -> DownloadSource:
        level = quality or "lossless"
        data = self._post("/weapi/song/enhance/player/url/v1", {
            "ids": f"[{track.remote_id}]",
            "level": level,
            "encodeType": "flac",
        })
        items = data.get("data") or []
        if not items:
            raise NoSource(f"网易云无返回音源: {track.title}")
        item = items[0]
        url = item.get("url")
        if not url:
            raise NoSource(f"网易云无可用音源({level}): {track.title} [{track.remote_id}]")
        # 试听片段检测: 未登录/无版权时 code=404 或返回 30s 片段
        if int(item.get("fee") or 0) in (1, 4) and not self.logged_in():
            raise NotLoggedIn("网易云 VIP 曲目需要登录 Cookie")
        ext = (item.get("type") or LEVEL_EXT.get(level, "mp3")).lower()
        return DownloadSource(
            url=url, ext=ext, quality=item.get("level") or level,
            bitrate=int(item.get("br") or 0), size=int(item.get("size") or 0),
        )

    def lyric(self, track: RemoteTrack) -> str:
        try:
            data = self._post("/weapi/song/lyric", {
                "id": track.remote_id, "lv": -1, "kv": -1, "tv": -1,
            })
        except ProviderError:
            return ""
        return ((data.get("lrc") or {}).get("lyric") or "").strip()

    # ---------- 写操作 ----------
    def add_tracks(self, remote_id: str, track_ids: list[str]) -> None:
        self._manipulate("add", remote_id, track_ids)

    def remove_tracks(self, remote_id: str, track_ids: list[str]) -> None:
        self._manipulate("del", remote_id, track_ids)

    def _manipulate(self, op: str, remote_id: str, track_ids: list[str]) -> None:
        if not self.logged_in():
            raise NotLoggedIn("网易云写操作需要登录 Cookie")
        if not track_ids:
            return
        data = self._post("/weapi/playlist/manipulate/tracks", {
            "op": op,
            "pid": str(remote_id),
            "trackIds": json.dumps([int(x) for x in track_ids]),
            "imme": "true",
        })
        code = data.get("code")
        if code not in (200, 201, None):
            raise ProviderError(f"网易云歌单{op}失败 code={code} msg={data.get('message')}")


# ---------- URL 识别 ----------
_PATTERNS = [
    re.compile(r"music\.163\.com/(?:m/)?playlist[/?]+(?:.*?[?&]id=)?(\d+)"),
    re.compile(r"[?&]id=(\d+)"),
]


def parse_playlist_id(url: str) -> str | None:
    text = (url or "").strip()
    if text.isdigit():
        return text
    if "playlist" in text:
        for pat in _PATTERNS:
            m = pat.search(text)
            if m:
                return m.group(1)
    return None
