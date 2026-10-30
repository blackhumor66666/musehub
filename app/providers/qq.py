"""QQ 音乐适配.

用到的都是 Web 端/旧版 Qzone 接口, 不需要 sign 计算:
  * 歌单详情  c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg
  * 用户歌单  c.y.qq.com/rsc/fcgi-bin/fcg_user_created_diss
  * 搜索      c.y.qq.com/soso/fcgi-bin/client_search_cp
  * 音源      u.y.qq.com/cgi-bin/musicu.fcg  (vkey.GetVkeyServer)

写操作(加/删歌单)用的是旧版 add2songdir 接口, 属于"尽力而为":
失败时抛 Unsupported, 由上层落进待处理队列交给人工, 不会静默丢动作.
"""
from __future__ import annotations

import json
import random
import re
import time
from urllib.parse import parse_qs, urlparse

import requests

from .base import (
    BaseProvider,
    DownloadSource,
    NoSource,
    NotLoggedIn,
    ProviderError,
    RemotePlaylist,
    RemoteTrack,
    Unsupported,
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REFERER = "https://y.qq.com/"

# 音质代号 -> (filename 前缀, 后缀)
QUALITY_MAP = {
    "m500": ("M500", "mp3"),
    "m800": ("M800", "mp3"),
    "c400": ("C400", "m4a"),
    "f000": ("F000", "flac"),
    "a000": ("A000", "ape"),
    "rs01": ("RS01", "flac"),
    "q000": ("Q000", "flac"),   # 臻品母带 2.0
    "ai00": ("AI00", "flac"),   # 臻品全景声
    "sb00": ("SB00", "flac"),   # 臻品母带 4.0 (实验)
    "hires": ("RS01", "flac"),
    "lossless": ("F000", "flac"),
    "exhigh": ("M800", "mp3"),
}


def _parse_cookie(cookie: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (cookie or "").split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out


class QQProvider(BaseProvider):
    name = "qq"
    display = "QQ音乐"

    def __init__(self, cfg, db=None):
        super().__init__(cfg, db)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Referer": REFERER})
        cookie = getattr(cfg, "cookie", "") or ""
        if cookie:
            self.session.headers["Cookie"] = cookie
        self.cookies = _parse_cookie(cookie)
        # guid 是 QQ 认的"设备号"。必须持久化: 每次重启都换一个 guid,
        # 会让 QQ 觉得是陌生设备, 影响取播放直链。
        self._guid = self._load_guid()
        self._fav_id: str | None = getattr(cfg, "favorite_dissid", "") or None

    def _load_guid(self) -> str:
        """取(或首次生成并保存)一个稳定的设备号."""
        if self.db is None:
            return str(random.randint(10**9, 10**10 - 1))
        saved = (self.db.kv_get("qq.guid", "") or "").strip()
        if saved.isdigit():
            return saved
        guid = str(random.randint(10**9, 10**10 - 1))
        self.db.kv_set("qq.guid", guid)
        return guid

    # ---------- 基础 ----------
    @property
    def uin(self) -> str:
        uin = getattr(self.cfg, "uin", "") or ""
        if uin:
            return uin
        raw = self.cookies.get("uin") or self.cookies.get("wxuin") or ""
        raw = raw.lstrip("o").lstrip("0")
        return raw if raw.isdigit() else "0"

    def logged_in(self) -> bool:
        return bool(self.cookies.get("qqmusic_key") or self.cookies.get("qm_keyst")
                    or self.cookies.get("uin") or self.cookies.get("wxuin"))

    def reload(self) -> None:
        cookie = getattr(self.cfg, "cookie", "") or ""
        if cookie:
            self.session.headers["Cookie"] = cookie
        else:
            self.session.headers.pop("Cookie", None)
        self.cookies = _parse_cookie(cookie)
        self._fav_id = getattr(self.cfg, "favorite_dissid", "") or None

    def status(self) -> str:
        if not self.logged_in():
            return "未登录(仅公开歌单/搜索)"
        return f"已登录 uin={self.uin}"

    def _get(self, url: str, params: dict, timeout: int = 20) -> dict:
        try:
            resp = self.session.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            text = resp.text.strip()
            if text.startswith("callback("):
                text = text[9:-1]
            return json.loads(text)
        except requests.RequestException as exc:
            raise ProviderError(f"QQ音乐请求失败: {exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"QQ音乐返回非 JSON: {exc}") from exc

    def _post_musicu(self, payload: dict, timeout: int = 20) -> dict:
        try:
            resp = self.session.post(
                "https://u.y.qq.com/cgi-bin/musicu.fcg", data=json.dumps(payload),
                timeout=timeout, headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise ProviderError(f"QQ音乐请求失败: {exc}") from exc
        except ValueError as exc:
            raise ProviderError(f"QQ音乐返回非 JSON: {exc}") from exc

    # ---------- 歌单 ----------
    def my_playlists(self) -> list[RemotePlaylist]:
        if self.uin == "0":
            raise NotLoggedIn("QQ音乐需要 Cookie 中的 uin")
        data = self._get("https://c.y.qq.com/rsc/fcgi-bin/fcg_user_created_diss", {
            "hostuin": self.uin, "format": "json", "inCharset": "utf8",
            "outCharset": "utf-8", "notice": 0, "platform": "yqq.json",
            "needNewCode": 0, "sin": 0, "size": 200,
        })
        body = data.get("data") or {}
        items = body.get("disslist") or []
        out = []
        for it in items:
            dissid = str(it.get("dissid") or "")
            if not dissid:
                continue
            out.append(RemotePlaylist(
                provider=self.name, remote_id=dissid,
                name=it.get("dissname") or "",
                cover=it.get("logo") or "",
                owner="me",
                is_favorite=bool(it.get("dissid") == self._fav_id),
            ))
        # 『我喜欢』不在创建列表里, 单独补一条(如果配置/探测到)
        if self._fav_id and all(p.remote_id != self._fav_id for p in out):
            out.insert(0, RemotePlaylist(
                provider=self.name, remote_id=self._fav_id,
                name="我喜欢", owner="me", is_favorite=True,
            ))
        return out

    def favorite_playlist_id(self) -> str | None:
        if self._fav_id:
            return self._fav_id
        # 尝试从歌单列表里找名字含"喜欢"的
        try:
            for pl in self.my_playlists():
                if "喜欢" in pl.name:
                    self._fav_id = pl.remote_id
                    return self._fav_id
        except ProviderError:
            pass
        return None

    def playlist(self, remote_id: str) -> RemotePlaylist:
        data = self._get(
            "https://c.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg",
            {
                "type": 1, "json": 1, "utf8": 1, "onlysong": 0,
                "disstid": str(remote_id), "format": "json", "g_tk": 5381,
                "loginUin": self.uin, "hostUin": 0, "inCharset": "utf8",
                "outCharset": "utf-8", "notice": 0, "platform": "yqq.json",
                "needNewCode": 0,
            },
        )
        cdlist = data.get("cdlist") or []
        if not cdlist:
            raise ProviderError(f"QQ音乐歌单 {remote_id} 不存在或不可见(可能需要 Cookie)")
        cd = cdlist[0]
        tracks = [self._to_track(s) for s in (cd.get("songlist") or []) if s.get("songmid")]
        return RemotePlaylist(
            provider=self.name, remote_id=str(cd.get("disstid") or remote_id),
            name=cd.get("dissname") or "", cover=cd.get("logo") or "",
            tracks=tracks, owner="", is_favorite=str(remote_id) == self._fav_id,
            declared_count=int(cd.get("songnum") or -1),
        )

    def _to_track(self, s: dict) -> RemoteTrack:
        album = s.get("album") or {}
        album_name = album.get("name") or s.get("albumname") or ""
        album_mid = album.get("mid") or s.get("albummid") or ""
        singers = s.get("singer") or []
        artists = [x.get("name", "") for x in singers if x.get("name")]
        year = ""
        pt = s.get("time_public") or ""
        if pt:
            m = re.match(r"(\d{4})", pt)
            year = m.group(1) if m else ""
        return RemoteTrack(
            provider=self.name,
            remote_id=str(s.get("songmid") or s.get("mid")),
            title=(s.get("songname") or s.get("name") or s.get("title") or "").strip(),
            artists=artists,
            album=album_name.strip(),
            album_artist=artists[0] if artists else "",
            duration=int(s.get("interval") or 0),
            year=year,
            track_no=int(s.get("index_album") or 0),
            cover_url=f"https://y.qq.com/music/photo_new/T002R500x500M000{album_mid}.jpg" if album_mid else "",
            raw={"songid": s.get("songid"), "albummid": album_mid},
        )

    # ---------- 专辑 / 榜单 / 登录态 ----------
    def track_detail(self, remote_id: str) -> RemoteTrack | None:
        """按歌曲 mid 取权威元数据(补年份/音轨号/专辑/封面)."""
        try:
            data = self._post_musicu({
                "comm": {"ct": 24, "cv": 0, "uin": self.uin or ""},
                "req": {"module": "music.pf_song_detail_svr", "method": "get_song_detail",
                        "param": {"song_mid": str(remote_id)}},
            })
        except ProviderError:
            return None
        info = ((data.get("req") or {}).get("data") or {}).get("track_info") or {}
        if not info.get("mid"):
            return None
        return self._to_track(info)

    def album(self, remote_id: str) -> RemotePlaylist:
        """专辑订阅(albumMid). 实测 2026-10: AlbumInfoServer.GetAlbumDetail 不再返回歌曲,
        能用的模块是 music.musichallAlbum.AlbumSongList.GetAlbumSongList."""
        data = self._post_musicu({
            "comm": {"ct": 24, "cv": 0, "uin": self.uin or ""},
            "req": {"module": "music.musichallAlbum.AlbumSongList",
                    "method": "GetAlbumSongList",
                    "param": {"albumMid": str(remote_id), "begin": 0, "num": 300, "order": 2}},
        })
        req = data.get("req") or {}
        if req.get("code") not in (0, "0", None):
            raise ProviderError(f"QQ音乐专辑 {remote_id} 拉取失败: code={req.get('code')}")
        body = req.get("data") or {}
        # 注意: 专辑接口的 songList 每项是 {"songInfo": {...}} 包了一层
        raw = [(s.get("songInfo") or s) for s in (body.get("songList") or [])]
        songs = [s for s in raw if s.get("mid") or s.get("songmid")]
        if not songs:
            raise ProviderError(f"QQ音乐专辑 {remote_id} 里没有歌曲(或 mid 不对)")
        tracks = [self._to_track(s) for s in songs]
        first_album = (songs[0].get("album") or {})
        return RemotePlaylist(
            provider=self.name,
            remote_id=str(remote_id),
            name=(first_album.get("name") or f"专辑 {remote_id}").strip(),
            cover=(f"https://y.qq.com/music/photo_new/T002R500x500M000{remote_id}.jpg"),
            tracks=tracks,
            owner=((songs[0].get("singer") or [{}])[0].get("name") or ""),
            kind="album",
            declared_count=int(body.get("totalNum") or len(tracks)),
        )

    def chart(self, remote_id: str) -> RemotePlaylist:
        """榜单订阅(topId, 如 26=热歌). musicu 的 toplist 模块返回 500003,
        实测可用的是老版 fcg_v8_toplist_cp."""
        data = self._get(
            "https://c.y.qq.com/v8/fcg-bin/fcg_v8_toplist_cp.fcg",
            {"topid": str(remote_id), "format": "json", "inCharset": "utf8",
             "outCharset": "utf-8", "notice": 0, "platform": "yqq.json",
             "needNewCode": 0, "uin": self.uin, "tpl": 3, "page": "detail", "type": "top"},
        )
        items = data.get("songlist") or []
        tracks: list[RemoteTrack] = []
        for it in items:
            s = it.get("data") or it
            if s.get("mid") or s.get("songmid"):
                tracks.append(self._to_track(s))
        if not tracks:
            raise ProviderError(f"QQ音乐榜单 {remote_id} 没有返回歌曲(topId 可能不对)")
        info = data.get("topinfo") or {}
        return RemotePlaylist(
            provider=self.name,
            remote_id=str(remote_id),
            name=(info.get("ListName") or f"榜单 {remote_id}").strip(),
            cover=info.get("pic_album") or "",
            tracks=tracks,
            kind="chart",
            declared_count=int(data.get("total_song_num") or len(tracks)),
        )

    def auth_check(self) -> tuple[bool, str]:
        """Cookie 是否还有效(拉自己的歌单需要登录态)."""
        if self.uin == "0":
            return False, "Cookie 里没有 uin，未登录"
        try:
            pls = self.my_playlists()
        except NotLoggedIn as exc:
            return False, f"Cookie 已失效: {str(exc)[:100]}"
        except ProviderError as exc:
            return False, f"接口不可达: {str(exc)[:120]}"
        if not pls:
            return False, "Cookie 已失效（拉不到任何歌单），请重新粘贴 QQ 音乐 Cookie"
        return True, ""

    # ---------- 搜索 ----------
    def search(self, keyword: str, limit: int = 20) -> list[RemoteTrack]:
        # 1) 新版接口(musicu.fcg). 2026-10 实测老接口 client_search_cp 已 500.
        try:
            data = self._post_musicu({
                "comm": {"ct": 19, "cv": 1859, "uin": self.uin or ""},
                "req": {
                    "method": "DoSearchForQQMusicDesktop",
                    "module": "music.search.SearchCgiService",
                    "param": {"search_type": 0, "query": keyword,
                              "page_num": 1, "num_per_page": limit},
                },
            })
            req = data.get("req") or {}
            # 注意 code=0 是 int, 不能写 `req.get("code") or -1`(0 会变 -1)
            if req.get("code") in (0, "0", None):
                songs = ((((req.get("data") or {}).get("body") or {})
                          .get("song") or {}).get("list")) or []
                if songs:
                    return [self._to_track(s) for s in songs if s.get("mid")]
        except ProviderError:
            pass
        # 2) 老接口兜底(近期不稳定, 返回结构新旧两版都兼容)
        base = {
            "ct": 24, "qqmusic_ver": 1298, "remoteplace": "txt.yqq.song",
            "searchid": int(time.time() * 1000), "t": 0, "aggr": 1, "cr": 1,
            "catZhida": 1, "lossless": 0, "flag_qc": 0, "p": 1, "n": limit,
            "w": keyword, "format": "json",
        }
        for extra in ({}, {"new_json": 1}):
            try:
                data = self._get("https://c.y.qq.com/soso/fcgi-bin/client_search_cp",
                                 {**base, **extra})
            except ProviderError:
                continue
            songs = ((data.get("data") or {}).get("song") or {}).get("list") or []
            if songs:
                return [self._to_track(s) for s in songs if s.get("songmid") or s.get("mid")]
        return []

    # ---------- 音源 ----------
    def source(self, track: RemoteTrack, quality: str) -> DownloadSource:
        code = (quality or "f000").lower()
        prefix, ext = QUALITY_MAP.get(code, ("F000", "flac"))
        mid = str(track.remote_id)
        filename = f"{prefix}{mid}{mid}.{ext}"
        payload = {
            "req": {
                "module": "CDN.SrfCdnDispatchServer",
                "method": "GetCdnDispatch",
                "param": {"guid": self._guid, "calltype": 0, "userip": ""},
            },
            "req_0": {
                "module": "vkey.GetVkeyServer",
                "method": "CgiGetVkey",
                "param": {
                    "guid": self._guid, "songmid": [mid], "songtype": [0],
                    "uin": self.uin, "loginflag": 1, "platform": "20",
                    "filename": [filename],
                },
            },
            "comm": {"uin": int(self.uin or 0), "format": "json", "ct": 24, "cv": 0},
        }
        data = self._post_musicu(payload)
        req0 = data.get("req_0") or {}
        if req0.get("code") not in (0, None):
            raise ProviderError(f"QQ音乐 vkey 失败 code={req0.get('code')}")
        info = ((req0.get("data") or {}).get("midurlinfo") or [{}])[0]
        purl = info.get("purl") or ""
        if not purl:
            raise NoSource(f"QQ音乐无可用音源({code}): {track.title} [{mid}]")
        sip = ((req0.get("data") or {}).get("sip") or ["https://ws.stream.qqmusic.qq.com/"])[0]
        return DownloadSource(
            url=sip.rstrip("/") + "/" + purl.lstrip("/"),
            ext=ext, quality=code, bitrate=0, size=0,
        )

    # ---------- 歌词 ----------
    def lyric(self, track: RemoteTrack) -> str:
        """QQ 歌词接口返回 base64, 这里统一解成纯文本 LRC."""
        import base64

        url = "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg"
        params = {
            "songmid": str(track.remote_id), "format": "json", "nobase64": 1,
            "g_tk": 5381, "loginUin": self.uin, "hostUin": 0,
            "inCharset": "utf8", "outCharset": "utf-8", "notice": 0,
            "platform": "yqq.json", "needNewCode": 0,
        }
        try:
            resp = self.session.get(url, params=params, timeout=15,
                                    headers={"Referer": "https://y.qq.com/portal/player.html"})
            text = resp.text.strip()
            if text.startswith("MusicJsonCallback"):
                text = text[text.index("(") + 1: text.rindex(")")]
            data = json.loads(text)
        except (requests.RequestException, ValueError):
            return ""
        raw = (data.get("lyric") or "").strip()
        if not raw:
            return ""
        if not raw.startswith("["):
            try:
                raw = base64.b64decode(raw).decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                pass
        return raw.replace("\r\n", "\n").strip()

    def _probe_vip(self) -> str:
        """判断 QQ 登录态与会员是否还能用于下载.

        两个踩过的坑:
          1. 周杰伦这类**数字专辑**需要单独购买(返回 104003), 拿它探测会把正常会员
             误判成"无会员" —— 所以改为用**库里自己下载过的歌**当参考。
          2. 歌单接口能用 ≠ 播放密钥能用: Cookie 里的 qm_keyst 过期后,
             收藏/歌单照常同步, 但所有直链都取不到。这时最该提示的是"更新 Cookie",
             而不是"你(用户)没有会员"。
        """
        from .base import RemoteTrack
        rows = []
        if self.db is not None:
            rows = [dict(r) for r in self.db.query(
                """SELECT remote_id, title FROM tracks
                   WHERE provider=? AND status='active' AND remote_id<>''
                   ORDER BY updated_at DESC LIMIT 3""", (self.name,))]
        if not rows:
            return "未能验证（库里还没有可从 QQ 取直链的曲目）"
        for r in rows:
            probe = RemoteTrack(provider=self.name, remote_id=r["remote_id"],
                                title=r["title"] or "参考曲目")
            for q in ("m800", "f000"):
                try:
                    self.source(probe, q)
                    return f"会员可用（参考曲目《{r['title']}》可取直链）"
                except Exception:  # noqa: BLE001 - 换档位/换曲目继续试
                    continue
        return ("Cookie 疑似失效：登录态还能读歌单，但所有直链都取不到。"
                "请到设置里重新粘贴 QQ 音乐 Cookie")

    # ---------- 写操作 (尽力而为) ----------
    def add_tracks(self, remote_id: str, track_ids: list[str]) -> None:
        self._songdir("add", remote_id, track_ids)

    def remove_tracks(self, remote_id: str, track_ids: list[str]) -> None:
        self._songdir("del", remote_id, track_ids)

    def _songdir(self, op: str, remote_id: str, track_ids: list[str]) -> None:
        if not self.logged_in():
            raise NotLoggedIn("QQ音乐写操作需要登录 Cookie")
        if not track_ids:
            return
        endpoint = ("https://c.y.qq.com/splcloud/fcgi-bin/fcg_music_add2songdir.fcg"
                    if op == "add" else
                    "https://c.y.qq.com/splcloud/fcgi-bin/fcg_music_del2songdir.fcg")
        data = {
            "mid": ",".join(str(x) for x in track_ids),
            "dirid": str(remote_id),
            "uin": self.uin,
            "loginUin": self.uin,
            "g_tk": 5381,
            "format": "json",
        }
        try:
            resp = self.session.post(endpoint, data=data, timeout=20)
        except Exception as exc:  # noqa: BLE001 - 旧接口失败形式多样
            raise Unsupported(f"QQ音乐歌单{op}接口不可用: {exc}") from exc
        if resp.status_code >= 400 or not (resp.text or "").strip().startswith("{"):
            # 实测该接口已返回 501 + "bad request", 说明这代 songdir 接口下线了。
            # 微信登录的 Cookie 也没有 skey/g_tk, 新版 musicu 写接口同样过不去。
            raise Unsupported(
                f"QQ 音乐不支持自动{('取消收藏' if op == 'del' else '收藏')}"
                f"（旧接口已下线, HTTP {resp.status_code}）；请在 QQ 音乐里手动处理")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise Unsupported(f"QQ音乐歌单{op}返回非 JSON: {exc}") from exc
        code = payload.get("code")
        if code not in (0, None):
            raise Unsupported(f"QQ音乐歌单{op}失败 code={code} ({payload.get('subcode')})")


# ---------- URL 识别 ----------
def parse_playlist_id(url: str) -> str | None:
    text = (url or "").strip()
    if text.isdigit():
        return text
    if "y.qq.com" in text:
        m = re.search(r"[?&#]id=(\w+)", text)
        if m:
            return m.group(1)
        m = re.search(r"/playlist/(\w+)", text)
        if m:
            return m.group(1)
    m = re.search(r"disstid=(\w+)", text)
    return m.group(1) if m else None
