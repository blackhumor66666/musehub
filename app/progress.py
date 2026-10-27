"""下载进度跟踪(内存态).

页面上的 "LIVE QUEUE / 正在下载" 就读这里. 断电丢失无所谓,
持久化的历史在 download_log 表里.
"""
from __future__ import annotations

import threading
import time
from typing import Any


class ProgressTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[int, dict[str, Any]] = {}
        self._history: list[dict[str, Any]] = []   # 最近完成的几条, 供页面补充展示
        self._seq = 0

    def start(self, track_id: int, provider: str, title: str, artists: str) -> int:
        with self._lock:
            self._seq += 1
            job = {
                "job": self._seq, "track_id": track_id, "provider": provider,
                "title": title, "artists": artists,
                "state": "queued", "downloaded": 0, "total": 0,
                "quality": "", "started_at": time.time(),
            }
            self._items[track_id] = job
            return job["job"]

    def update(self, track_id: int, **fields: Any) -> None:
        with self._lock:
            job = self._items.get(track_id)
            if not job:
                return
            job.update(fields)

    def progress(self, track_id: int, downloaded: int, total: int) -> None:
        with self._lock:
            job = self._items.get(track_id)
            if not job:
                return
            job["state"] = "downloading"
            job["downloaded"] = downloaded
            job["total"] = total

    def finish(self, track_id: int, ok: bool, error: str = "", quality: str = "") -> None:
        with self._lock:
            job = self._items.pop(track_id, None)
            if not job:
                return
            job["state"] = "done" if ok else "failed"
            job["error"] = error[:200]
            job["quality"] = quality
            job["finished_at"] = time.time()
            self._history.insert(0, job)
            del self._history[20:]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            active = sorted(self._items.values(), key=lambda x: x["job"])
            return {
                "active": [dict(j) for j in active],
                "recent": [dict(j) for j in self._history[:8]],
                "count": len(active),
            }
