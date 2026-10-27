"""Navidrome 适配层测试(不联网, 用假的 HTTP 层复刻 Navidrome 0.64 的真实怪行为).

被复刻的三个坑(都是实测出来的, 换版本可能变):
  1. POST /api/playlist 的 songIds 字段被静默忽略 -> 必须再调 /tracks
  2. GET  /api/playlist/{id} 不返回曲目 -> 必须走 /tracks, 且歌曲 id 在 mediaFileId
  3. DELETE /api/playlist/{id}/tracks 不生效 -> 只能走 Subsonic songIdToRemove
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.navidrome import Navidrome, NavidromeError  # noqa: E402


class _Resp:
    def __init__(self, payload, status: int = 200):
        self._payload = payload
        self.status_code = status
        self.text = "" if payload is None else str(payload)[:200]
        self.content = b"x" if payload is not None else b""

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class FakeNavidrome:
    """内存版 Navidrome, 只实现本项目用到的那几个端点."""

    def __init__(self) -> None:
        self.token = "tok-1"
        self.playlists: dict[str, dict] = {}   # id -> {"name":..., "songs":[...]}
        self._seq = 0
        self.calls: list[tuple[str, str]] = []

    def _new_id(self, prefix: str = "pl") -> str:
        self._seq += 1
        return f"{prefix}{self._seq}"

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def request(self, method: str, url: str, headers=None, params=None, json=None, **_kw):
        parts = urlsplit(url)
        path = parts.path
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        query.update({k: (v[0] if isinstance(v, list) else v) for k, v in (params or {}).items()})
        self.calls.append((method, path))

        # --- 登录 ---
        if path == "/auth/login":
            if (json or {}).get("password") != "pw":
                return _Resp({"error": "Invalid username or password"}, 401)
            return _Resp({"token": self.token})

        # --- Subsonic ---
        if path.startswith("/rest/"):
            name = path[len("/rest/"):].replace(".view", "")
            if name == "ping":
                return _Resp({"subsonic-response": {"status": "ok"}})
            if name == "updatePlaylist":
                pl = self.playlists.get(query.get("playlistId", ""))
                if pl is None:
                    return _Resp({"subsonic-response": {"status": "failed"}})
                # 复刻 0.64.2 的真实行为: songIdToRemove 返回 ok 但**什么都不删**
                idx = query.get("songIndexToRemove")
                if idx not in (None, ""):
                    try:
                        pl["songs"].pop(int(idx))
                    except (IndexError, ValueError):
                        pass
                sid = query.get("songIdToAdd")
                if sid:
                    pl["songs"].append(sid)
                return _Resp({"subsonic-response": {"status": "ok"}})
            if name == "getPlaylists":
                return _Resp({"subsonic-response": {
                    "status": "ok",
                    "playlists": {"playlist": [
                        {"id": k, "name": v["name"], "songCount": len(v["songs"])}
                        for k, v in self.playlists.items()]}}})
            return _Resp({"subsonic-response": {"status": "failed"}})

        # --- native API ---
        if headers and headers.get("x-nd-authorization") != f"Bearer {self.token}":
            return _Resp({"error": "unauthorized"}, 401)

        if path == "/api/playlist" and method == "GET":
            return _Resp([{"id": k, "name": v["name"], "songCount": len(v["songs"])}
                          for k, v in self.playlists.items()])
        if path == "/api/playlist" and method == "POST":
            pid = self._new_id()
            # 关键: 这里故意忽略 json 里的 songIds, 复刻 Navidrome 的真实行为
            self.playlists[pid] = {"name": (json or {}).get("name", ""), "songs": []}
            return _Resp({"id": pid})

        if path.startswith("/api/playlist/"):
            rest = path[len("/api/playlist/"):]
            if rest.endswith("/tracks"):
                pid = rest[: -len("/tracks")]
                pl = self.playlists.get(pid)
                if pl is None:
                    return _Resp({"error": "not found"}, 404)
                if method == "GET":
                    return _Resp([{"id": str(i + 1), "mediaFileId": s}
                                  for i, s in enumerate(pl["songs"])])
                if method == "POST":
                    added = 0
                    for sid in (json or {}).get("ids", []):
                        if sid not in pl["songs"]:
                            pl["songs"].append(sid)
                            added += 1
                    return _Resp({"added": added})
                if method == "DELETE":
                    # 复刻: 0.64 上这个端点不生效
                    return _Resp({"ids": None})
            pid = rest
            pl = self.playlists.get(pid)
            if pl is None:
                return _Resp({"error": "not found"}, 404)
            if method == "GET":
                # 复刻: 不返回 tracks 字段
                return _Resp({"id": pid, "name": pl["name"], "songCount": len(pl["songs"])})
            if method == "PUT":
                if (json or {}).get("name"):
                    pl["name"] = json["name"]
                return _Resp({"id": pid, "name": pl["name"]})
            if method == "DELETE":
                self.playlists.pop(pid, None)
                return _Resp(None)
        return _Resp({"error": f"unhandled {method} {path}"}, 404)


class TestNavidrome(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeNavidrome()
        cfg = SimpleNamespace(url="http://nd.local", username="admin", password="pw",
                              playlist_prefix="")
        self.nd = Navidrome(cfg)
        self.nd._session = self.fake  # type: ignore[assignment]

    def test_create_playlist_actually_stores_songs(self) -> None:
        pid = self.nd.create_playlist("测试", ["s1", "s2", "s3"])
        self.assertEqual(self.fake.playlists[pid]["songs"], ["s1", "s2", "s3"])
        self.assertEqual(len(self.nd.get_playlist_song_ids(pid)), 3)

    def test_get_playlist_song_ids_uses_media_file_id(self) -> None:
        """条目 id 是 1/2/3, 歌曲 id 在 mediaFileId —— 必须取对."""
        pid = self.nd.create_playlist("x", ["aa", "bb"])
        ids = self.nd.get_playlist_song_ids(pid)
        self.assertEqual(ids, ["aa", "bb"])
        self.assertNotIn("1", ids)

    def test_replace_adds_and_removes_incrementally(self) -> None:
        pid = self.nd.create_playlist("x", ["s1", "s2", "s3"])
        res = self.nd.replace_playlist(pid, "x", ["s2", "s3", "s4"])
        self.assertEqual(res["added"], 1)
        self.assertEqual(res["removed"], 1)
        self.assertEqual(sorted(self.fake.playlists[pid]["songs"]), ["s2", "s3", "s4"])

    def test_rename_and_delete_playlist(self) -> None:
        """改名要生效; 删除后歌单必须真的从播放器里消失(清理残留的依据)."""
        pid = self.nd.create_playlist("旧名字", ["s1"])
        self.nd.rename_playlist(pid, "新名字")
        self.assertEqual(self.fake.playlists[pid]["name"], "新名字")
        self.nd.delete_playlist(pid)
        self.assertNotIn(pid, self.fake.playlists)
        # 再删一次不报错(幂等)
        self.nd.delete_playlist(pid)

    def test_replace_is_idempotent(self) -> None:
        pid = self.nd.create_playlist("x", ["s1", "s2"])
        res = self.nd.replace_playlist(pid, "x", ["s1", "s2"])
        self.assertTrue(res["unchanged"])
        self.assertEqual(self.fake.playlists[pid]["songs"], ["s1", "s2"])

    def test_for_account_returns_isolated_client(self) -> None:
        """多账号: for_account 必须返回独立客户端(独立 session/token), 否则歌单会建到同一个 owner 名下."""
        other = self.nd.for_account("alice", "pw2")
        self.assertIsNot(other, self.nd)
        self.assertEqual(other.cfg.username, "alice")
        self.assertIsNone(other._token, "新客户端不能复用默认账号的 token")
        # 同一个账号则复用自身
        self.assertIs(self.nd.for_account("admin", "pw"), self.nd)

    def test_remove_uses_index_not_songid(self) -> None:
        """0.64.2 上 songIdToRemove 静默失效, 必须按索引删(否则歌单只增不减)."""
        pid = self.nd.create_playlist("x", ["s1", "s2", "s3", "s4"])
        removed = self.nd.remove_playlist_tracks(pid, ["s2", "s4"])
        self.assertEqual(removed, 2)
        self.assertEqual(self.fake.playlists[pid]["songs"], ["s1", "s3"])

    def test_replace_keeps_playlist_id(self) -> None:
        """增量更新不能重建歌单(id 变了会让播放器里的链接失效)."""
        pid = self.nd.create_playlist("x", ["s1"])
        self.nd.replace_playlist(pid, "x", ["s1", "s2"])
        self.assertIn(pid, self.fake.playlists)

    def test_sync_playlist_reuses_same_name(self) -> None:
        index = {"a/1.flac": "s1", "a/2.flac": "s2"}
        r1 = self.nd.sync_playlist("[网易云] 我的歌单", ["a/1.flac", "a/2.flac"], index)
        self.assertEqual(r1["added"], 2)
        r2 = self.nd.sync_playlist("[网易云] 我的歌单", ["a/1.flac", "a/2.flac"], index)
        self.assertTrue(r2["unchanged"])
        self.assertEqual(r1["playlist_id"], r2["playlist_id"])
        self.assertEqual(len(self.fake.playlists), 1, "同名歌单不该重复创建")

    def test_sync_playlist_reports_missing(self) -> None:
        r = self.nd.sync_playlist("x", ["a/1.flac", "nope/2.flac"], {"a/1.flac": "s1"})
        self.assertEqual(r["missing"], 1)
        self.assertEqual(r["songs"], 1)

    def test_login_failure_raises(self) -> None:
        """密码错时 native 登录必须抛错, 且 probe 要如实报告(而不是假装成功)."""
        cfg = SimpleNamespace(url="http://nd.local", username="admin", password="wrong",
                              playlist_prefix="")
        nd = Navidrome(cfg)
        nd._session = self.fake  # type: ignore[assignment]
        with self.assertRaises(NavidromeError):
            nd._login(force=True)
        report = nd.probe()
        self.assertFalse(report["native"])
        self.assertIn("native_error", report)


if __name__ == "__main__":
    unittest.main()
