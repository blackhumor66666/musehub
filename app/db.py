"""SQLite 数据层.

只用一个文件库, WAL 模式, 所有写入串行化. 表结构围绕四个核心概念:

  tracks         本地已知的曲目 (含来源平台的唯一 key)
  playlists      远端歌单 <-> 本地镜像歌单 的映射
  playlist_items 歌单成员关系 (多对多, 用于判断"这首歌还有没有被引用")
  actions        需要向平台回推的动作队列 (取消收藏等), 失败可重试
  events         人类可读的运行日志 (TG/Web 直接读它)
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    provider     TEXT NOT NULL,
    remote_id    TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    artists      TEXT NOT NULL DEFAULT '',      -- '; ' 分隔
    album        TEXT NOT NULL DEFAULT '',
    albumartist  TEXT NOT NULL DEFAULT '',
    duration     INTEGER NOT NULL DEFAULT 0,    -- 秒
    year         TEXT NOT NULL DEFAULT '',
    track_no     INTEGER NOT NULL DEFAULT 0,
    cover        TEXT NOT NULL DEFAULT '',      -- 封面图 URL
    account      TEXT NOT NULL DEFAULT '',      -- 下载所用账号(多账号)
    ext          TEXT NOT NULL DEFAULT '',
    quality      TEXT NOT NULL DEFAULT '',
    path         TEXT NOT NULL DEFAULT '',      -- 相对 MUSIC_DIR
    size         INTEGER NOT NULL DEFAULT 0,
    managed      INTEGER NOT NULL DEFAULT 1,    -- 1=本程序下载, 0=用户原有文件
    status       TEXT NOT NULL DEFAULT 'active',-- active|missing|deleted|failed
    attempts     INTEGER NOT NULL DEFAULT 0,    -- 下载失败重试次数
    fail_reason  TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT '',      -- ''|playlist|search|local
    created_at   TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL DEFAULT '',
    UNIQUE(provider, remote_id)
);
CREATE INDEX IF NOT EXISTS idx_tracks_path ON tracks(path);
CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks(status);

CREATE TABLE IF NOT EXISTS playlists (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    provider       TEXT NOT NULL,
    remote_id      TEXT NOT NULL,
    name           TEXT NOT NULL DEFAULT '',
    cover          TEXT NOT NULL DEFAULT '',
    track_count    INTEGER NOT NULL DEFAULT 0,
    subscribed     INTEGER NOT NULL DEFAULT 1,
    navidrome_name TEXT NOT NULL DEFAULT '',
    navidrome_id   TEXT NOT NULL DEFAULT '',
    account        TEXT NOT NULL DEFAULT '',     -- 歌单所属账号(多账号)
    kind           TEXT NOT NULL DEFAULT 'playlist', -- playlist|album|chart
    display_name   TEXT NOT NULL DEFAULT '',     -- 用户自定义显示名(空=用平台名)
    mirror_account TEXT NOT NULL DEFAULT '',     -- 镜像到哪个播放器账号(空=默认)
    last_sync_at   TEXT NOT NULL DEFAULT '',
    last_error     TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL DEFAULT '',
    UNIQUE(provider, remote_id)
);

CREATE TABLE IF NOT EXISTS playlist_items (
    playlist_id  INTEGER NOT NULL,
    track_id     INTEGER NOT NULL,
    position     INTEGER NOT NULL DEFAULT 0,
    seen_at      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (playlist_id, track_id)
);
CREATE INDEX IF NOT EXISTS idx_items_track ON playlist_items(track_id);

CREATE TABLE IF NOT EXISTS actions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,          -- unfavorite|favorite|playlist_remove|playlist_add|navidrome_sync
    provider   TEXT NOT NULL DEFAULT '',
    payload    TEXT NOT NULL DEFAULT '{}',
    state      TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|failed|skipped
    attempts   INTEGER NOT NULL DEFAULT 0,
    next_at    REAL NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_actions_state ON actions(state, next_at);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL DEFAULT '',
    level   TEXT NOT NULL DEFAULT 'info',
    source  TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL DEFAULT '',
    meta    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);
-- 体检里"补不了"的条目: 用户点忽略后不再重复提示
-- kind: cover / lyric / meta
CREATE TABLE IF NOT EXISTS ignored (
    kind       TEXT NOT NULL,
    track_id   INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (kind, track_id)
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger     TEXT NOT NULL DEFAULT 'manual',
    state       TEXT NOT NULL DEFAULT 'running',
    started_at  TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    stats       TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS download_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT '',
    provider    TEXT NOT NULL DEFAULT '',
    remote_id   TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    artists     TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'ok',     -- ok | fail
    quality     TEXT NOT NULL DEFAULT '',
    ext         TEXT NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    elapsed_ms  INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_dl_ts ON download_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_dl_status ON download_log(status);
"""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """给老库补列, 保证升级不丢数据."""
        track_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(tracks)")}
        pl_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(playlists)")}
        for col, ddl in (
            ("attempts", "ALTER TABLE tracks ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"),
            ("cover", "ALTER TABLE tracks ADD COLUMN cover TEXT NOT NULL DEFAULT ''"),
            ("account", "ALTER TABLE tracks ADD COLUMN account TEXT NOT NULL DEFAULT ''"),
            ("quality", "ALTER TABLE tracks ADD COLUMN quality TEXT NOT NULL DEFAULT ''"),
            # 来源: '' 或 'playlist'(歌单同步) / 'search'(面板搜歌) / 'local'(库内纳管)
            ("source", "ALTER TABLE tracks ADD COLUMN source TEXT NOT NULL DEFAULT ''"),
        ):
            if col not in track_cols:
                self._conn.execute(ddl)
        if "account" not in pl_cols:
            self._conn.execute("ALTER TABLE playlists ADD COLUMN account TEXT NOT NULL DEFAULT ''")
        # 订阅类型: playlist(歌单) / album(专辑) / chart(榜单)
        if "kind" not in pl_cols:
            self._conn.execute(
                "ALTER TABLE playlists ADD COLUMN kind TEXT NOT NULL DEFAULT 'playlist'")
        # 用户自定义的显示名(留空 = 用平台名); 播放器里的镜像歌单用它命名
        if "display_name" not in pl_cols:
            self._conn.execute(
                "ALTER TABLE playlists ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")
        # 镜像到哪个 Navidrome 账号(留空 = 默认账号); 私有歌单只有 owner 看得见
        if "mirror_account" not in pl_cols:
            self._conn.execute(
                "ALTER TABLE playlists ADD COLUMN mirror_account TEXT NOT NULL DEFAULT ''")

    # ---------- 基础设施 ----------
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, [tuple(r) for r in rows])
            self._conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if row is None:
            return default
        val = row[0]
        return default if val is None else val

    # ---------- kv ----------
    def kv_get(self, key: str, default: str = "") -> str:
        row = self.query_one("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else default

    def kv_set(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---------- 体检忽略清单 ----------
    def ignore_items(self, kind: str, track_ids: Iterable[int]) -> int:
        ids = [int(i) for i in track_ids]
        if not ids:
            return 0
        ts = now_iso()
        with self.tx() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO ignored(kind, track_id, created_at) VALUES(?,?,?)",
                [(kind, i, ts) for i in ids],
            )
        return len(ids)

    def ignored_ids(self) -> dict[str, set[int]]:
        out: dict[str, set[int]] = {}
        for r in self.query("SELECT kind, track_id FROM ignored"):
            out.setdefault(r["kind"], set()).add(int(r["track_id"]))
        return out

    def ignored_count(self) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM ignored", (), 0))

    def clear_ignored(self, kind: str = "") -> int:
        if kind:
            return int(self.execute("DELETE FROM ignored WHERE kind=?", (kind,)).rowcount or 0)
        return int(self.execute("DELETE FROM ignored").rowcount or 0)

    # ---------- events ----------
    def log(self, level: str, source: str, message: str, **meta: Any) -> None:
        self.execute(
            "INSERT INTO events(ts, level, source, message, meta) VALUES(?,?,?,?,?)",
            (now_iso(), level, source, message[:2000], json.dumps(meta, ensure_ascii=False)[:4000]),
        )
        # 保留最近 5000 条, 防止日志表无限膨胀(每 200 次写入才检查一次, 开销可忽略)
        self._log_writes = getattr(self, "_log_writes", 0) + 1
        if self._log_writes % 200 == 0:
            try:
                self.execute("DELETE FROM events WHERE id <= "
                             "(SELECT COALESCE(MAX(id),0)-5000 FROM events)")
            except Exception:  # noqa: BLE001 - 清理失败不影响主流程
                pass

    def recent_events(self, limit: int = 50, level: str | None = None) -> list[dict]:
        if level:
            rows = self.query("SELECT * FROM events WHERE level=? ORDER BY id DESC LIMIT ?", (level, limit))
        else:
            rows = self.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["meta"] = json.loads(d["meta"])
            except Exception:
                d["meta"] = {}
            out.append(d)
        return out

    # ---------- tracks ----------
    # 这些字段"空值不能覆盖已有值":
    # 平台列表接口经常不给年份/音轨号/专辑, 若用空串覆盖, 会把刮削补全的结果冲掉
    # (线上踩过: 每次同步歌单/重启, 补好的年份全被清空)。
    META_NO_BLANK = ("title", "artists", "album", "albumartist", "duration",
                     "year", "track_no", "cover", "quality", "ext", "size")

    @staticmethod
    def _blank(v: Any) -> bool:
        if v is None:
            return True
        if isinstance(v, (int, float)):
            return int(v) == 0
        return not str(v).strip()

    def upsert_track(self, t: dict) -> int:
        """按 (provider, remote_id) 幂等写入, 返回本地 track id."""
        existing_row = self.query_one(
            "SELECT * FROM tracks WHERE provider=? AND remote_id=?",
            (t["provider"], t["remote_id"]),
        )
        existing = dict(existing_row) if existing_row is not None else None
        ts = now_iso()
        if existing:
            fields = []
            params: list[Any] = []
            for key in ("title", "artists", "album", "albumartist", "duration", "year",
                        "track_no", "cover", "account", "ext", "quality", "path", "size",
                        "managed", "status", "fail_reason", "source"):
                if key in t and t[key] is not None:
                    # 空值不覆盖已有值(顺序: 先判断新值是否为空)
                    if key in self.META_NO_BLANK and self._blank(t[key]) \
                            and not self._blank(existing.get(key)):
                        continue
                    fields.append(f"{key}=?")
                    params.append(t[key])
            if fields:
                params.extend([ts, existing["id"]])
                self.execute(f"UPDATE tracks SET {', '.join(fields)}, updated_at=? WHERE id=?", params)
            return int(existing["id"])
        cur = self.execute(
            """INSERT INTO tracks(provider, remote_id, title, artists, album, albumartist,
                duration, year, track_no, cover, account, ext, quality, path, size, managed,
                status, fail_reason, source, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                t["provider"], t["remote_id"], t.get("title", ""), t.get("artists", ""),
                t.get("album", ""), t.get("albumartist", ""), int(t.get("duration") or 0),
                t.get("year", ""), int(t.get("track_no") or 0), t.get("cover", ""),
                t.get("account", ""), t.get("ext", ""),
                t.get("quality", ""), t.get("path", ""), int(t.get("size") or 0),
                int(t.get("managed", 1)), t.get("status", "active"), t.get("fail_reason", ""),
                t.get("source", ""), ts, ts,
            ),
        )
        return int(cur.lastrowid)

    def get_track(self, track_id: int) -> dict | None:
        row = self.query_one("SELECT * FROM tracks WHERE id=?", (track_id,))
        return dict(row) if row else None

    def find_track(self, provider: str, remote_id: str) -> dict | None:
        row = self.query_one(
            "SELECT * FROM tracks WHERE provider=? AND remote_id=?", (provider, str(remote_id))
        )
        return dict(row) if row else None

    def tracks_by_status(self, status: str) -> list[dict]:
        return [dict(r) for r in self.query("SELECT * FROM tracks WHERE status=?", (status,))]

    def active_paths(self) -> set[str]:
        return {r["path"] for r in self.query("SELECT path FROM tracks WHERE status='active' AND path<>''")}

    def cleanup_orphans(self) -> int:
        """删除"不属于任何歌单"的待下载/失败记录(歌单被删后的残留元数据).

        例外: `source='search'` 是用户在面板里手动搜歌下载的, 本来就不属于任何歌单,
        失败记录必须留着(否则用户看不到失败原因、也没法重试)。
        """
        cur = self.execute(
            """DELETE FROM tracks
               WHERE status IN ('pending','failed')
                 AND COALESCE(source,'') <> 'search'
                 AND NOT EXISTS (SELECT 1 FROM playlist_items pi WHERE pi.track_id = tracks.id)"""
        )
        return cur.rowcount or 0

    def drop_playlist(self, row_id: int) -> None:
        """移除歌单: 清成员关系 + 歌单行 + 孤儿曲目(已下载文件不动)."""
        with self.tx() as conn:
            conn.execute("DELETE FROM playlist_items WHERE playlist_id=?", (row_id,))
            conn.execute("DELETE FROM playlists WHERE id=?", (row_id,))
        self.cleanup_orphans()

    def track_ref_count(self, track_id: int) -> int:
        """这首歌被多少个已订阅歌单引用 (决定能不能删)."""
        return int(self.scalar(
            """SELECT COUNT(*) FROM playlist_items pi
               JOIN playlists p ON p.id = pi.playlist_id
               WHERE pi.track_id=? AND p.subscribed=1""",
            (track_id,), 0,
        ))

    def stats(self) -> dict:
        def one(sql: str, params: Iterable[Any] = ()) -> int:
            return int(self.scalar(sql, params, 0))

        return {
            "tracks_total": one("SELECT COUNT(*) FROM tracks"),
            "tracks_active": one("SELECT COUNT(*) FROM tracks WHERE status='active'"),
            # 真实文件数(去重复用会让多条记录指向同一个文件, 记录数会虚高)
            "tracks_unique_files": one(
                "SELECT COUNT(DISTINCT path) FROM tracks WHERE status='active' AND path<>''"),
            "tracks_missing": one("SELECT COUNT(*) FROM tracks WHERE status='missing'"),
            "tracks_failed": one("SELECT COUNT(*) FROM tracks WHERE status='failed'"),
            "playlists": one("SELECT COUNT(*) FROM playlists WHERE subscribed=1"),
            "actions_pending": one("SELECT COUNT(*) FROM actions WHERE state='queued'"),
            "actions_failed": one("SELECT COUNT(*) FROM actions WHERE state='failed'"),
            "last_run": dict(self.query_one("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1") or {}),
        }

    # ---------- 曲库统计 ----------
    def library_stats(self) -> dict:
        """按**真实文件**统计曲库(同一 path 的多条记录只算一次).

        面板上"媒体库曲目"必须用这个数字: 跨平台同曲会复用同一个文件,
        按记录数算会把一首歌算成两三首。
        """
        rows = [dict(r) for r in self.query(
            """SELECT path,
                      MAX(size) AS size, MAX(duration) AS duration,
                      MAX(ext) AS ext, MAX(quality) AS quality,
                      MAX(artists) AS artists, MAX(albumartist) AS albumartist,
                      MAX(album) AS album, MAX(year) AS year,
                      MAX(managed) AS managed
               FROM tracks
               WHERE status='active' AND path<>''
               GROUP BY path"""
        )]
        files = len(rows)
        total_bytes = sum(int(r["size"] or 0) for r in rows)
        total_seconds = sum(int(r["duration"] or 0) for r in rows)

        by_ext: dict[str, dict] = {}
        by_quality: dict[str, int] = {}
        by_artist: dict[str, int] = {}
        by_album: dict[tuple[str, str], int] = {}
        by_decade: dict[str, int] = {}
        managed = 0

        for r in rows:
            ext = (r["ext"] or "?").lower()
            slot = by_ext.setdefault(ext, {"ext": ext, "count": 0, "bytes": 0})
            slot["count"] += 1
            slot["bytes"] += int(r["size"] or 0)

            q = (r["quality"] or "").strip() or "unknown"
            by_quality[q] = by_quality.get(q, 0) + 1

            artist = (r["artists"] or "").split(";")[0].strip() or (r["albumartist"] or "未知艺人")
            by_artist[artist] = by_artist.get(artist, 0) + 1

            album = (r["album"] or "").strip() or "单曲"
            key = (album, artist)
            by_album[key] = by_album.get(key, 0) + 1

            year = (r["year"] or "").strip()[:4]
            if year.isdigit() and 1900 <= int(year) <= 2100:
                decade = f"{int(year) // 10 * 10}s"
                by_decade[decade] = by_decade.get(decade, 0) + 1

            if int(r["managed"] or 0) == 1:
                managed += 1

        missing_files = int(self.scalar(
            "SELECT COUNT(DISTINCT path) FROM tracks WHERE status='missing' AND path<>''",
            (), 0))
        lossless_ext = {"flac", "ape", "wav", "aiff", "wv"}
        lossless_files = sum(
            1 for r in rows if (r["ext"] or "").lower() in lossless_ext)

        def top(counter: dict, n: int) -> list[dict]:
            items = sorted(counter.items(), key=lambda kv: -kv[1])[:n]
            return [{"name": k, "count": v} for k, v in items]

        albums = sorted(by_album.items(), key=lambda kv: -kv[1])[:10]
        return {
            "files": files,
            "total_bytes": total_bytes,
            "total_seconds": total_seconds,
            "lossless_files": lossless_files,
            "lossy_files": files - lossless_files,
            "missing_files": missing_files,
            "managed_files": managed,          # 本程序下载的
            "external_files": files - managed,  # 用户原有/其他工具放入
            "by_ext": sorted(by_ext.values(), key=lambda x: -x["count"]),
            "by_quality": sorted(
                ({"quality": k, "count": v} for k, v in by_quality.items()),
                key=lambda x: -x["count"]),
            "top_artists": top(by_artist, 12),
            "top_albums": [{"album": a, "artist": ar, "count": n} for (a, ar), n in albums],
            "by_decade": [{"decade": k, "count": v}
                          for k, v in sorted(by_decade.items())],
        }

    # ---------- playlists ----------
    def upsert_playlist(self, provider: str, remote_id: str, name: str, cover: str = "",
                        track_count: int = 0, account: str = "",
                        kind: str = "playlist") -> int:
        ts = now_iso()
        row = self.query_one(
            "SELECT id FROM playlists WHERE provider=? AND remote_id=?", (provider, str(remote_id))
        )
        if row:
            self.execute(
                "UPDATE playlists SET name=?, cover=?, track_count=?, kind=? WHERE id=?",
                (name, cover, track_count, kind, row["id"]),
            )
            return int(row["id"])
        cur = self.execute(
            """INSERT INTO playlists(provider, remote_id, name, cover, track_count,
                                     navidrome_name, account, kind, created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (provider, str(remote_id), name, cover, track_count,
             f"{name}", account, kind, ts),
        )
        return int(cur.lastrowid)

    def get_playlist(self, row_id: int) -> dict | None:
        row = self.query_one("SELECT * FROM playlists WHERE id=?", (row_id,))
        return dict(row) if row else None

    def subscribed_playlists(self) -> list[dict]:
        return [dict(r) for r in self.query("SELECT * FROM playlists WHERE subscribed=1 ORDER BY id")]

    def set_playlist_items(self, playlist_id: int, track_ids: list[int]) -> None:
        ts = now_iso()
        with self.tx() as conn:
            conn.execute("DELETE FROM playlist_items WHERE playlist_id=?", (playlist_id,))
            conn.executemany(
                "INSERT OR REPLACE INTO playlist_items(playlist_id, track_id, position, seen_at) VALUES(?,?,?,?)",
                [(playlist_id, tid, i, ts) for i, tid in enumerate(track_ids)],
            )

    def playlist_track_ids(self, playlist_id: int) -> list[int]:
        rows = self.query(
            "SELECT track_id FROM playlist_items WHERE playlist_id=? ORDER BY position", (playlist_id,)
        )
        return [int(r["track_id"]) for r in rows]

    def drop_playlist_items(self, playlist_id: int, track_ids: list[int]) -> None:
        if not track_ids:
            return
        with self.tx() as conn:
            conn.executemany(
                "DELETE FROM playlist_items WHERE playlist_id=? AND track_id=?",
                [(playlist_id, tid) for tid in track_ids],
            )

    # ---------- actions ----------
    def enqueue_action(self, kind: str, provider: str, payload: dict, delay: float = 0.0) -> int:
        ts = now_iso()
        # 同一类动作去重: 同一个 (kind, provider, payload 关键字段) 只留一条 queued
        dedup_key = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        existing = self.query_one(
            "SELECT id FROM actions WHERE kind=? AND provider=? AND payload=? AND state='queued'",
            (kind, provider, dedup_key),
        )
        if existing:
            return int(existing["id"])
        cur = self.execute(
            """INSERT INTO actions(kind, provider, payload, state, attempts, next_at, created_at, updated_at)
               VALUES(?,?,?,'queued',0,?,?,?)""",
            (kind, provider, dedup_key, time.time() + delay, ts, ts),
        )
        return int(cur.lastrowid)

    def claim_actions(self, limit: int = 10) -> list[dict]:
        now = time.time()
        with self.tx() as conn:
            rows = conn.execute(
                "SELECT * FROM actions WHERE state='queued' AND next_at<=? ORDER BY id LIMIT ?",
                (now, limit),
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                conn.executemany(
                    "UPDATE actions SET state='running', updated_at=? WHERE id=?",
                    [(now_iso(), i) for i in ids],
                )
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d["payload"])
            except Exception:
                d["payload"] = {}
            out.append(d)
        return out

    def finish_action(self, action_id: int, ok: bool, error: str = "", retry_after: float | None = None) -> None:
        row = self.query_one("SELECT attempts FROM actions WHERE id=?", (action_id,))
        attempts = int(row["attempts"]) + 1 if row else 1
        ts = now_iso()
        if ok:
            self.execute(
                "UPDATE actions SET state='done', attempts=?, last_error='', updated_at=? WHERE id=?",
                (attempts, ts, action_id),
            )
            return
        if retry_after is None or attempts >= 5:
            self.execute(
                "UPDATE actions SET state='failed', attempts=?, last_error=?, updated_at=? WHERE id=?",
                (attempts, error[:500], ts, action_id),
            )
        else:
            self.execute(
                """UPDATE actions SET state='queued', attempts=?, last_error=?, next_at=?, updated_at=?
                   WHERE id=?""",
                (attempts, error[:500], time.time() + retry_after, ts, action_id),
            )

    def pending_actions(self, limit: int = 100) -> list[dict]:
        rows = self.query(
            "SELECT * FROM actions WHERE state IN ('queued','running','failed') ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d["payload"])
            except Exception:
                d["payload"] = {}
            out.append(d)
        return out

    # ---------- download_log ----------
    def log_download(self, provider: str, remote_id: str, title: str, artists: str,
                     status: str, quality: str = "", ext: str = "", size: int = 0,
                     elapsed_ms: int = 0, error: str = "") -> None:
        self.execute(
            """INSERT INTO download_log(ts, provider, remote_id, title, artists, status,
                                        quality, ext, size, elapsed_ms, error)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (now_iso(), provider, str(remote_id), title[:300], artists[:300], status,
             quality, ext, int(size), int(elapsed_ms), error[:500]),
        )

    def download_history(self, provider: str = "", status: str = "",
                         page: int = 1, size: int = 30) -> dict:
        where, params = [], []
        if provider:
            where.append("provider=?")
            params.append(provider)
        if status:
            where.append("status=?")
            params.append(status)
        w = ("WHERE " + " AND ".join(where)) if where else ""
        total = int(self.scalar(f"SELECT COUNT(*) FROM download_log {w}", params, 0))
        rows = self.query(
            f"SELECT * FROM download_log {w} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, size, max(0, (page - 1)) * size),
        )
        return {"total": total, "page": page, "size": size,
                "items": [dict(r) for r in rows]}

    def download_summary(self) -> dict:
        """今日统计 + 近 7 天趋势 + 平台分布."""
        today = time.strftime("%Y-%m-%d")
        # 按曲目去重计数: 同一首歌重试 3 次失败只算 1 首, 否则"今日失败"会虚高
        DISTINCT_TRACK = "COUNT(DISTINCT provider || ':' || remote_id)"
        ok_today = int(self.scalar(
            f"SELECT {DISTINCT_TRACK} FROM download_log WHERE status='ok' AND ts LIKE ?",
            (f"{today}%",), 0))
        fail_today = int(self.scalar(
            f"SELECT {DISTINCT_TRACK} FROM download_log WHERE status='fail' AND ts LIKE ?",
            (f"{today}%",), 0))
        bytes_today = int(self.scalar(
            "SELECT COALESCE(SUM(size),0) FROM download_log WHERE status='ok' AND ts LIKE ?",
            (f"{today}%",), 0))
        trend = []
        for i in range(6, -1, -1):
            day = time.strftime("%Y-%m-%d", time.localtime(time.time() - i * 86400))
            ok = int(self.scalar(
                f"SELECT {DISTINCT_TRACK} FROM download_log WHERE status='ok' AND ts LIKE ?",
                (f"{day}%",), 0))
            fail = int(self.scalar(
                f"SELECT {DISTINCT_TRACK} FROM download_log WHERE status='fail' AND ts LIKE ?",
                (f"{day}%",), 0))
            trend.append({"day": day[5:], "ok": ok, "fail": fail})
        by_platform = [
            {"provider": r["provider"], "count": int(r["c"])}
            for r in self.query(
                f"SELECT provider, {DISTINCT_TRACK} AS c FROM download_log WHERE status='ok' "
                "AND ts >= ? GROUP BY provider", (today[:8] + "01",),
            )
        ]
        total = ok_today + fail_today
        return {
            "ok_today": ok_today, "fail_today": fail_today,
            "bytes_today": bytes_today,
            "rate": round(ok_today * 100.0 / total, 1) if total else None,
            "trend": trend, "by_platform": by_platform,
        }

    # ---------- sync runs ----------
    def start_run(self, trigger: str) -> int:
        cur = self.execute(
            "INSERT INTO sync_runs(trigger, state, started_at) VALUES(?, 'running', ?)",
            (trigger, now_iso()),
        )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, stats: dict, state: str = "done") -> None:
        self.execute(
            "UPDATE sync_runs SET state=?, finished_at=?, stats=? WHERE id=?",
            (state, now_iso(), json.dumps(stats, ensure_ascii=False), run_id),
        )
