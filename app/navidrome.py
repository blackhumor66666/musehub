"""Navidrome 适配.

优先走 Navidrome 自己的 REST API (能一次拿到全库 path->songId 映射),
失败则退回 Subsonic 协议 (兼容性最好, 但只能按关键词搜歌).

上层只关心两件事:
  index_songs()                 -> {相对路径: songId}
  sync_playlist(name, paths)    -> 让远端歌单内容等于给定路径集合
"""
from __future__ import annotations

import dataclasses
import hashlib
import random
import re
import string
import time
import unicodedata
from types import SimpleNamespace
from typing import Any

import requests


def _norm_path(p: str) -> str:
    p = unicodedata.normalize("NFC", (p or "").strip())
    p = p.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


class NavidromeError(Exception):
    pass


class Navidrome:
    def __init__(self, cfg, db=None):
        self.cfg = cfg
        self.db = db
        self.base = (cfg.url or "").rstrip("/")
        self._token: str | None = None
        self._token_at: float = 0.0
        self._native_ok: bool | None = None
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "MuseHub/0.1"})

    # ---------- 可用性 ----------
    def available(self) -> bool:
        return bool(self.base and self.cfg.username and self.cfg.password)

    def reconfigure(self) -> None:
        """面板改了地址/账号后重建连接状态."""
        self.base = (self.cfg.url or "").rstrip("/")
        self._token = None
        self._token_at = 0.0
        self._native_ok = None
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "MuseHub/0.1"})

    def probe(self) -> dict[str, Any]:
        """自检: 分别报告 native API 与 Subsonic 是否可用."""
        out: dict[str, Any] = {"url": self.base, "native": False, "subsonic": False, "version": ""}
        if not self.available():
            out["error"] = "未配置 NAVIDROME_URL/USERNAME/PASSWORD"
            return out
        try:
            self._login(force=True)
            out["native"] = True
        except NavidromeError as exc:
            out["native_error"] = str(exc)
        try:
            data = self._sub("ping")
            out["subsonic"] = data.get("status") == "ok"
            out["version"] = ((data.get("version") or "") if isinstance(data, dict) else "")
        except NavidromeError as exc:
            out["subsonic_error"] = str(exc)
        return out

    # ---------- 认证 ----------
    def _login(self, force: bool = False) -> str:
        if self._token and not force and time.time() - self._token_at < 6 * 3600:
            return self._token
        if not self.available():
            raise NavidromeError("Navidrome 未配置")
        try:
            resp = self._session.post(
                f"{self.base}/auth/login",
                json={"username": self.cfg.username, "password": self.cfg.password},
                timeout=15,
            )
        except requests.RequestException as exc:
            raise NavidromeError(f"连接 Navidrome 失败: {exc}") from exc
        if resp.status_code != 200:
            raise NavidromeError(f"Navidrome 登录失败 HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise NavidromeError("Navidrome 登录返回非 JSON") from exc
        token = data.get("token") or data.get("jwt") or ""
        if not token:
            raise NavidromeError("Navidrome 登录未返回 token")
        self._token = token
        self._token_at = time.time()
        self._native_ok = True
        return token

    def _nd(self, method: str, path: str, **kwargs) -> Any:
        token = self._login()
        headers = kwargs.pop("headers", {})
        headers["x-nd-authorization"] = f"Bearer {token}"
        url = f"{self.base}{path}"
        try:
            resp = self._session.request(method, url, headers=headers, timeout=30, **kwargs)
        except requests.RequestException as exc:
            raise NavidromeError(f"{method} {path} 失败: {exc}") from exc
        if resp.status_code == 401:
            # token 过期, 重试一次
            self._token = None
            token = self._login(force=True)
            headers["x-nd-authorization"] = f"Bearer {token}"
            resp = self._session.request(method, url, headers=headers, timeout=30, **kwargs)
        if resp.status_code >= 400:
            raise NavidromeError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:200]}")
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError:
            return resp.text

    # ---------- Subsonic ----------
    def _sub_params(self) -> dict[str, str]:
        salt = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        token = hashlib.md5((self.cfg.password + salt).encode()).hexdigest()
        return {
            "u": self.cfg.username, "t": token, "s": salt,
            "v": "1.16.1", "c": "MuseHub", "f": "json",
        }

    def _sub(self, method: str, **params) -> dict:
        if not self.available():
            raise NavidromeError("Navidrome 未配置")
        query = self._sub_params()
        query.update({k: v for k, v in params.items() if v is not None})
        try:
            resp = self._session.get(
                f"{self.base}/rest/{method}.view", params=query, timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise NavidromeError(f"Subsonic {method} 失败: {exc}") from exc
        except ValueError as exc:
            raise NavidromeError(f"Subsonic {method} 返回非 JSON") from exc
        body = data.get("subsonic-response") or {}
        if body.get("status") != "ok":
            err = body.get("error") or {}
            raise NavidromeError(f"Subsonic {method} 错误: {err.get('message') or err}")
        return body

    # ---------- 曲库索引 ----------
    def index_songs(self, page_size: int = 500, max_pages: int = 200) -> dict[str, str]:
        """返回 {相对路径: songId}. 走 native API, 失败退回 Subsonic search3."""
        if not self.available():
            return {}
        try:
            return self._index_native(page_size, max_pages)
        except NavidromeError:
            return self._index_subsonic()

    def _index_native(self, page_size: int, max_pages: int) -> dict[str, str]:
        out: dict[str, str] = {}
        start = 0
        for _ in range(max_pages):
            batch = self._nd(
                "GET", "/api/song",
                params={"_start": start, "_end": start + page_size, "_sort": "title", "_order": "ASC"},
            )
            if not isinstance(batch, list) or not batch:
                break
            for song in batch:
                sid = song.get("id")
                path = song.get("path") or ""
                if sid and path:
                    out[_norm_path(path)] = str(sid)
            if len(batch) < page_size:
                break
            start += page_size
        if not out:
            raise NavidromeError("native /api/song 返回空")
        return out

    def _index_subsonic(self) -> dict[str, str]:
        """兜底: 用 search3 逐首搜, 只用于歌单同步时按需查."""
        return {}

    def find_song_id(self, index: dict[str, str], rel_path: str) -> str | None:
        key = _norm_path(rel_path)
        if key in index:
            return index[key]
        # 大小写不敏感兜底
        low = key.casefold()
        for k, v in index.items():
            if k.casefold() == low:
                return v
        # 只按文件名兜底(目录结构被用户改过)
        base = key.rsplit("/", 1)[-1].casefold()
        hits = [v for k, v in index.items() if k.rsplit("/", 1)[-1].casefold() == base]
        return hits[0] if len(hits) == 1 else None

    def search_song_id(self, title: str, artist: str) -> str | None:
        """Subsonic 兜底搜索."""
        try:
            data = self._sub("search3", query=f"{artist} {title}".strip(), songCount=5)
        except NavidromeError:
            return None
        songs = ((data.get("searchResult3") or {}).get("song") or [])
        if isinstance(songs, dict):
            songs = [songs]
        for s in songs:
            if (s.get("title") or "").casefold() == (title or "").casefold():
                return str(s.get("id"))
        return str(songs[0]["id"]) if songs and songs[0].get("id") else None

    # ---------- 歌单 ----------
    def for_account(self, username: str, password: str) -> "Navidrome":
        """以另一个 Navidrome 账号身份操作(多用户隔离用).

        Navidrome 的私有歌单只有创建者(和 admin)看得见, 所以"不同用户看到不同歌单"
        只能靠**以该用户身份创建歌单**。这里返回一个独立 session/token 的客户端。
        """
        if not username or username == self.cfg.username:
            return self
        if dataclasses.is_dataclass(self.cfg):
            cfg = dataclasses.replace(self.cfg, username=username, password=password)
        else:  # 测试里可能传 SimpleNamespace
            cfg = SimpleNamespace(**{**vars(self.cfg), "username": username,
                                     "password": password})
        return Navidrome(cfg, self.db)

    def list_playlists(self) -> list[dict]:
        try:
            data = self._nd("GET", "/api/playlist", params={"_start": 0, "_end": 500})
            if isinstance(data, list):
                return data
        except NavidromeError:
            pass
        body = self._sub("getPlaylists")
        pls = (body.get("playlists") or {}).get("playlist") or []
        if isinstance(pls, dict):
            pls = [pls]
        return [{"id": p.get("id"), "name": p.get("name"), "songCount": p.get("songCount")} for p in pls]

    def get_playlist_song_ids(self, playlist_id: str) -> list[str]:
        """歌单里的歌曲 id 列表.

        坑: Navidrome 0.64 的 GET /api/playlist/{id} 不返回曲目,
        必须走 /tracks; 而且 /tracks 返回的 `id` 是"歌单条目 id",
        真正的歌曲 id 在 `mediaFileId`.
        """
        try:
            data = self._nd("GET", f"/api/playlist/{playlist_id}/tracks")
            if isinstance(data, list):
                return [str(t["mediaFileId"]) for t in data if t.get("mediaFileId")]
        except NavidromeError:
            pass
        body = self._sub("getPlaylist", id=playlist_id)
        entries = ((body.get("playlist") or {}).get("entry") or [])
        if isinstance(entries, dict):
            entries = [entries]
        return [str(e.get("id")) for e in entries if e.get("id")]

    def add_playlist_tracks(self, playlist_id: str, song_ids: list[str]) -> int:
        """批量加歌: native POST /tracks 一次搞定, 失败再逐个走 Subsonic."""
        ids = [str(s) for s in song_ids if s]
        if not ids:
            return 0
        try:
            res = self._nd("POST", f"/api/playlist/{playlist_id}/tracks",
                           json={"ids": ids})
            if isinstance(res, dict) and "added" in res:
                return int(res.get("added") or 0)
        except NavidromeError:
            pass
        added = 0
        for sid in ids:
            try:
                self._sub("updatePlaylist", playlistId=playlist_id, songIdToAdd=sid)
                added += 1
            except NavidromeError:
                continue
        return added

    def remove_playlist_tracks(self, playlist_id: str, song_ids: list[str]) -> int:
        """移除歌曲.

        坑(0.64.2 实测): Subsonic 的 `songIdToRemove` 返回 status=ok 但**什么都不删**,
        native DELETE /tracks 返回 ids:null 也不生效。只有 `songIndexToRemove`
        (按位置删)真的有效 —— 所以先取当前列表, 把要删的歌换算成下标,
        从大到小删(避免前面的删除让后面下标位移)。
        """
        ids = [str(s) for s in song_ids if s]
        if not ids:
            return 0
        want = set(ids)
        current = self.get_playlist_song_ids(playlist_id)
        positions = sorted((i for i, sid in enumerate(current) if sid in want), reverse=True)
        removed = 0
        for idx in positions:
            try:
                self._sub("updatePlaylist", playlistId=playlist_id, songIndexToRemove=idx)
                removed += 1
            except NavidromeError:
                continue
        return removed

    def create_playlist(self, name: str, song_ids: list[str]) -> str:
        try:
            data = self._nd("POST", "/api/playlist", json={
                "name": name, "comment": "MuseHub 自动同步", "public": False,
            })
            if isinstance(data, dict) and data.get("id"):
                pid = str(data["id"])
                if song_ids:
                    self.add_playlist_tracks(pid, song_ids)
                return pid
        except NavidromeError:
            pass
        body = self._sub("createPlaylist", name=name, **{"songId": song_ids or None})
        pid = ((body.get("playlist") or {}).get("id"))
        if not pid:
            raise NavidromeError("创建歌单失败")
        return str(pid)

    def rename_playlist(self, playlist_id: str, name: str) -> None:
        try:
            self._nd("PUT", f"/api/playlist/{playlist_id}", json={"name": name})
        except NavidromeError:
            pass

    def replace_playlist(self, playlist_id: str, name: str, song_ids: list[str]) -> dict:
        """让远端歌单内容等于 song_ids, 只动差异(不整体重建, 保留歌单 id)."""
        current = self.get_playlist_song_ids(playlist_id)
        if current == song_ids:
            return {"added": 0, "removed": 0, "unchanged": True}
        cur_set, tgt_set = set(current), set(song_ids)
        to_del = [x for x in current if x not in tgt_set]
        to_add = [x for x in song_ids if x not in cur_set]
        removed = self.remove_playlist_tracks(playlist_id, to_del)
        added = self.add_playlist_tracks(playlist_id, to_add)
        if name:
            self.rename_playlist(playlist_id, name)
        return {"added": added, "removed": removed,
                "unchanged": not to_add and not to_del}

    def delete_playlist(self, playlist_id: str) -> None:
        """删除歌单. 幂等: 歌单已不存在(404)视为成功, 这样清理残留不会误报."""
        try:
            self._nd("DELETE", f"/api/playlist/{playlist_id}")
            return
        except NavidromeError as exc:
            if "404" in str(exc):
                return
        self._sub("deletePlaylist", id=playlist_id)

    def start_scan(self) -> bool:
        try:
            self._nd("POST", "/api/scanner/scan")
            return True
        except NavidromeError:
            pass
        try:
            self._sub("startScan")
            return True
        except NavidromeError:
            return False

    # ---------- 高层 ----------
    def sync_playlist(self, name: str, rel_paths: list[str], index: dict[str, str]) -> dict:
        """把远端歌单内容同步为 rel_paths 对应的歌曲集合. 返回统计."""
        song_ids: list[str] = []
        missing: list[str] = []
        for rel in rel_paths:
            sid = self.find_song_id(index, rel)
            if sid:
                if sid not in song_ids:
                    song_ids.append(sid)
            else:
                missing.append(rel)

        existing = {str(p.get("name")): p for p in self.list_playlists()}
        target = existing.get(name)
        if target and target.get("id"):
            pid = str(target["id"])
            res = self.replace_playlist(pid, name, song_ids)
            return {"playlist_id": pid, "added": res.get("added", 0),
                    "removed": res.get("removed", 0),
                    "unchanged": res.get("unchanged", False),
                    "songs": len(song_ids), "missing": len(missing)}
        pid = self.create_playlist(name, song_ids)
        return {"playlist_id": pid, "added": len(song_ids), "removed": 0,
                "unchanged": False, "songs": len(song_ids), "missing": len(missing)}
