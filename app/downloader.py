"""下载器: 音质回退 + 完整性校验 + 断点清理.

设计要点:
  * 先落 staging, 校验通过才进音乐库, 库里不会出现半个文件;
  * 每个平台按 quality_chain 依次尝试, 前一个没版权/没权限就降级;
  * 下载完用 mutagen 打开一次, 读不出时长视为坏文件, 直接丢弃重试;
  * 并发受信号量限制, 避免把家用宽带和平台接口打爆.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import requests

from .providers.base import DownloadSource, NoSource, NotLoggedIn, ProviderError, RemoteTrack

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MIN_AUDIO_BYTES = 64 * 1024


class DownloadError(Exception):
    pass


class Downloader:
    def __init__(self, cfg, db, providers: dict):
        self.cfg = cfg
        self.db = db
        self.providers = providers
        self._sem = threading.Semaphore(cfg.sync.download_concurrency)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": UA})

    # ---------- 对外 ----------
    def fetch(self, provider_name: str, track: RemoteTrack,
              on_progress=None) -> tuple[Path, DownloadSource]:
        """下载到 staging, 返回 (本地文件, 音源信息). 失败抛 DownloadError."""
        provider = self.providers.get(provider_name)
        if provider is None:
            raise DownloadError(f"未知平台 {provider_name}")

        chain = list(getattr(provider.cfg, "quality_chain", ()) or ())
        if not chain:
            chain = ["standard"]
        errors: list[str] = []
        for quality in chain:
            try:
                src = provider.source(track, quality)
            except NotLoggedIn as exc:
                raise DownloadError(str(exc)) from exc
            except NoSource as exc:
                errors.append(str(exc))
                continue
            except ProviderError as exc:
                errors.append(str(exc))
                continue
            try:
                path = self._download(track, src, on_progress)
                return path, src
            except DownloadError as exc:
                errors.append(f"{quality}: {exc}")
                continue
        detail = "; ".join(errors[-3:]) or "无可用音源"
        if errors and all("无可用音源" in e or "无返回音源" in e for e in errors):
            detail += " ｜ 该账号下这首歌没有任何可用音质(通常是会员专享/地区限制)"
        raise DownloadError(detail)

    def fetch_image(self, url: str, timeout: int = 20) -> bytes:
        if not url:
            return b""
        try:
            resp = self._session.get(url, timeout=timeout)
            if resp.status_code == 200 and len(resp.content) > 1024:
                return resp.content
        except requests.RequestException:
            pass
        return b""

    # ---------- 内部 ----------
    def _download(self, track: RemoteTrack, src: DownloadSource, on_progress=None) -> Path:
        with self._sem:
            staging = self.cfg.staging_dir / track.provider
            staging.mkdir(parents=True, exist_ok=True)
            tmp = staging / f"{track.remote_id}.part"
            final = staging / f"{track.remote_id}.{src.ext}"

            headers = {"User-Agent": UA}
            headers.update(src.headers or {})
            if track.provider == "qq":
                headers.setdefault("Referer", "https://y.qq.com/")

            last_error = ""
            for attempt in range(3):
                try:
                    self._stream_to(src.url, tmp, headers,
                                    on_progress=on_progress if attempt == 0 else None)
                    size = tmp.stat().st_size
                    if size < MIN_AUDIO_BYTES:
                        raise DownloadError(f"文件过小({size}B), 可能是试听片段或错误页")
                    if src.size and size < src.size * 0.9:
                        raise DownloadError(f"文件不完整({size}/{src.size}B)")
                    if not self._probe(tmp, track):
                        raise DownloadError("音频无法解析, 疑似损坏")
                    if final.exists():
                        final.unlink()
                    os.replace(tmp, final)
                    return final
                except (requests.RequestException, DownloadError, OSError) as exc:
                    last_error = str(exc)
                    tmp.unlink(missing_ok=True)
                    if attempt < 2:
                        time.sleep(1.5 * (attempt + 1))
            raise DownloadError(last_error or "下载失败")

    def _stream_to(self, url: str, dest: Path, headers: dict, on_progress=None) -> None:
        with self._session.get(url, stream=True, timeout=(10, 60), headers=headers) as resp:
            if resp.status_code != 200:
                raise DownloadError(f"HTTP {resp.status_code}")
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype or "application/json" in ctype:
                raise DownloadError(f"返回的不是音频({ctype})")
            total = int(resp.headers.get("Content-Length") or 0)
            written = 0
            last_report = 0.0
            with open(dest, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    written += len(chunk)
                    if on_progress and time.time() - last_report > 0.4:
                        last_report = time.time()
                        on_progress(written, total)
                    if written > 300 * 1024 * 1024:
                        raise DownloadError("文件超过 300MB, 疑似异常")
            if on_progress:
                on_progress(written, total or written)

    @staticmethod
    def _probe(path: Path, track: RemoteTrack) -> bool:
        try:
            from mutagen import File as MFile
            audio = MFile(str(path))
            if audio is None or not getattr(audio, "info", None):
                return False
            length = float(getattr(audio.info, "length", 0) or 0)
            if length < 5:
                return False
            # 与平台标注时长偏差过大 -> 可能是别的歌/试听
            if track.duration and abs(length - track.duration) > max(30, track.duration * 0.35):
                return False
            return True
        except Exception:  # noqa: BLE001
            return False
