"""文件整理: 命名规范 + 标签写入 + 封面/歌词落盘.

规范目标:
  1. 对 Navidrome 友好 —— 标签优先, 目录结构稳定;
  2. 对 Music Tag Web 友好 —— 艺术家/专辑目录干净, 便于按专辑批量刮削;
  3. 对 SMB 共享友好 —— 文件名不含 Windows 非法字符, 组件长度受限.

默认落盘形态:
    <艺人>/<专辑>/<艺人> - <歌名>.flac
    <艺人>/单曲/<艺人> - <歌名>.flac        # 专辑名缺失或等于歌名时

多艺人处理: 目录与 albumartist 只取第一艺人, artist 标签用 "; " 连接.
这样不会再出现 TGmusicBot 那种 "David GuettaSia" 的目录名.
"""
from __future__ import annotations

import os
import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path

# Windows/SMB 非法字符
_INVALID = r'[<>:"/\\|?*\x00-\x1f]'
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
AUDIO_EXT = {".flac", ".mp3", ".m4a", ".ape", ".wav", ".ogg", ".aac", ".wma", ".dsf", ".dff"}


def sanitize_component(name: str, max_bytes: int = 120) -> str:
    """把任意字符串变成安全的单层路径组件."""
    s = unicodedata.normalize("NFC", (name or "").strip())
    s = re.sub(_INVALID, "_", s)
    s = re.sub(r"\s+", " ", s)
    s = s.strip().strip(".")          # 结尾的点和空格在 SMB 上会被吞掉
    s = s.strip()
    if not s:
        return "_"
    if s.split(".")[0].lower() in _RESERVED:
        s = "_" + s
    # 按字节截断, 避免多字节字符被切坏
    raw = s.encode("utf-8")
    if len(raw) > max_bytes:
        s = raw[:max_bytes].decode("utf-8", "ignore").rstrip(" ._-")
        s = s or "_"
    return s


@dataclass
class TrackMeta:
    title: str
    artists: list[str]
    album: str = ""
    albumartist: str = ""
    year: str = ""
    track_no: int = 0
    disc_no: int = 0
    duration: int = 0

    @property
    def artist_str(self) -> str:
        return "; ".join(a for a in self.artists if a) or "未知艺术家"

    @property
    def primary_artist(self) -> str:
        return next((a for a in self.artists if a), "未知艺术家")

    @property
    def effective_albumartist(self) -> str:
        return (self.albumartist or "").strip() or self.primary_artist

    @property
    def is_single(self) -> bool:
        al = (self.album or "").strip()
        if not al:
            return True
        return al.casefold() == (self.title or "").strip().casefold()


class Organizer:
    def __init__(self, cfg):
        self.cfg = cfg

    # ---------- 路径 ----------
    def relative_path(self, meta: TrackMeta, ext: str) -> Path:
        """根据模板生成相对 MUSIC_DIR 的路径(已 sanitize)."""
        single = meta.is_single
        template = self.cfg.single_template if single else self.cfg.album_template
        album = self.cfg.single_album_name if single else meta.album
        values = {
            "albumartist": meta.effective_albumartist,
            "artist": meta.primary_artist,
            "artists": meta.artist_str,
            "album": album,
            "title": meta.title,
            "year": meta.year or "未知年份",
            "track": f"{meta.track_no:02d}" if meta.track_no else "00",
            "disc": meta.disc_no or 1,
        }
        try:
            rendered = template.format(**values)
        except (KeyError, IndexError, ValueError):
            rendered = f"{values['albumartist']}/{values['album']}/{values['albumartist']} - {values['title']}"

        parts = [p for p in re.split(r"[\\/]+", rendered) if p.strip()]
        safe_parts = [sanitize_component(p, self.cfg.min_component_bytes) for p in parts]
        if not safe_parts:
            safe_parts = ["未知艺术家", "未分类", sanitize_component(meta.title)]
        filename = safe_parts[-1]
        ext = (ext or "flac").lstrip(".").lower()
        if not filename.lower().endswith("." + ext):
            filename = f"{filename}.{ext}"
        safe_parts[-1] = sanitize_component(filename[:-len(ext) - 1], self.cfg.min_component_bytes) + "." + ext
        return Path(*safe_parts)

    def target_path(self, meta: TrackMeta, ext: str, music_dir: Path) -> Path:
        """解析出最终绝对路径, 处理同名冲突."""
        rel = self.relative_path(meta, ext)
        target = music_dir / rel
        if not target.exists():
            return target
        # 已存在: 若像是同一首歌(同目录同前缀), 直接复用
        stem = target.stem
        n = 2
        while target.exists():
            candidate = target.with_name(f"{stem} ({n}){target.suffix}")
            if not candidate.exists():
                return candidate
            n += 1
            if n > 50:
                return target.with_name(f"{stem} ({abs(hash(str(target))) % 10000}){target.suffix}")
        return target

    # ---------- 落盘 ----------
    # 音乐库是 SMB 共享给 Windows 用的, 权限必须"所有人都能读写",
    # 否则 Windows 里打不开/改不了。与现有音乐库(777)保持一致。
    FILE_MODE = 0o666
    DIR_MODE = 0o777

    def place(self, src: Path, meta: TrackMeta, music_dir: Path) -> Path:
        """把暂存文件移动到最终位置, 返回绝对路径."""
        target = self.target_path(meta, src.suffix, music_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        if src.resolve() == target.resolve():
            self.fix_perms(target, music_dir)
            return target
        # 跨设备(NAS/SMB)也能用
        shutil.move(str(src), str(target))
        self.fix_perms(target, music_dir)
        return target

    def fix_perms(self, path: Path, music_dir: Path | None = None) -> None:
        """放开文件与新建目录的权限.

        必须做: mutagen 保存标签会**重建**文件, 新文件权限是 700 root:root,
        SMB 用户(非 root)就读不到也改不了。容器里是 root, 所以只能由我们来 chmod。
        """
        try:
            os.chmod(path, self.FILE_MODE)
        except OSError:
            return
        stop = music_dir.resolve() if music_dir else None
        parent = path.parent
        while parent and parent != parent.parent:
            if stop and parent.resolve() == stop:
                break
            try:
                # 无条件设置(幂等): 目录可能刚被建出来, 也可能是之前留下的 700
                os.chmod(parent, self.DIR_MODE)
            except OSError:
                pass
            parent = parent.parent

    # ---------- 标签 ----------
    def write_tags(self, path: Path, meta: TrackMeta) -> None:
        ext = path.suffix.lower()
        try:
            if ext == ".flac":
                self._tags_flac(path, meta)
            elif ext == ".mp3":
                self._tags_id3(path, meta)
            elif ext in (".m4a", ".mp4", ".aac"):
                self._tags_mp4(path, meta)
            elif ext in (".ape", ".wv"):
                self._tags_ape(path, meta)
            elif ext == ".wav":
                self._tags_id3(path, meta, wave=True)
            elif ext == ".ogg":
                self._tags_ogg(path, meta)
        except Exception as exc:  # noqa: BLE001 - 标签失败不应阻断入库
            raise RuntimeError(f"写标签失败 {path.name}: {exc}") from exc

    def _tags_flac(self, path: Path, meta: TrackMeta) -> None:
        from mutagen.flac import FLAC
        audio = FLAC(str(path))
        audio["title"] = meta.title
        audio["artist"] = meta.artist_str
        audio["albumartist"] = meta.effective_albumartist
        if not meta.is_single:
            audio["album"] = meta.album
        else:
            audio["album"] = self.cfg.single_album_name
        if meta.year:
            audio["date"] = meta.year
        if meta.track_no:
            audio["tracknumber"] = str(meta.track_no)
        if meta.disc_no:
            audio["discnumber"] = str(meta.disc_no)
        audio.save()

    def _tags_id3(self, path: Path, meta: TrackMeta, wave: bool = False) -> None:
        from mutagen.id3 import (
            APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TRCK,
        )
        if wave:
            from mutagen.wave import WAVE
            audio = WAVE(str(path))
            if audio.tags is None:
                audio.add_tags()
        else:
            from mutagen.mp3 import MP3
            audio = MP3(str(path))
            if audio.tags is None:
                audio.add_tags()
        tags: ID3 = audio.tags
        tags.setall("TIT2", [TIT2(encoding=3, text=[meta.title])])
        tags.setall("TPE1", [TPE1(encoding=3, text=[meta.artist_str])])
        tags.setall("TPE2", [TPE2(encoding=3, text=[meta.effective_albumartist])])
        tags.setall("TALB", [TALB(encoding=3, text=[
            meta.album if not meta.is_single else self.cfg.single_album_name
        ])])
        if meta.year:
            tags.setall("TDRC", [TDRC(encoding=3, text=[meta.year])])
        if meta.track_no:
            tags.setall("TRCK", [TRCK(encoding=3, text=[str(meta.track_no)])])
        audio.save()

    def _tags_mp4(self, path: Path, meta: TrackMeta) -> None:
        from mutagen.mp4 import MP4
        audio = MP4(str(path))
        if audio.tags is None:
            audio.add_tags()
        audio["\xa9nam"] = [meta.title]
        audio["\xa9ART"] = [meta.artist_str]
        audio["aART"] = [meta.effective_albumartist]
        audio["\xa9alb"] = [meta.album if not meta.is_single else self.cfg.single_album_name]
        if meta.year:
            audio["\xa9day"] = [meta.year]
        if meta.track_no:
            audio["trkn"] = [(meta.track_no, 0)]
        audio.save()

    def _tags_ape(self, path: Path, meta: TrackMeta) -> None:
        from mutagen.apev2 import APEv2
        audio = APEv2(str(path))
        audio["Title"] = meta.title
        audio["Artist"] = meta.artist_str
        audio["Album Artist"] = meta.effective_albumartist
        audio["Album"] = meta.album if not meta.is_single else self.cfg.single_album_name
        if meta.year:
            audio["Year"] = meta.year
        if meta.track_no:
            audio["Track"] = str(meta.track_no)
        audio.save()

    def _tags_ogg(self, path: Path, meta: TrackMeta) -> None:
        from mutagen.oggvorbis import OggVorbis
        audio = OggVorbis(str(path))
        audio["title"] = meta.title
        audio["artist"] = meta.artist_str
        audio["albumartist"] = meta.effective_albumartist
        audio["album"] = meta.album if not meta.is_single else self.cfg.single_album_name
        if meta.year:
            audio["date"] = meta.year
        if meta.track_no:
            audio["tracknumber"] = str(meta.track_no)
        audio.save()

    # ---------- 封面 ----------
    def embed_cover(self, path: Path, image: bytes) -> None:
        if not image:
            return
        ext = path.suffix.lower()
        try:
            if ext == ".flac":
                from mutagen.flac import FLAC, Picture
                audio = FLAC(str(path))
                audio.clear_pictures()
                pic = Picture()
                pic.type = 3
                pic.mime = self._mime(image)
                pic.desc = "Cover"
                pic.data = image
                audio.add_picture(pic)
                audio.save()
            elif ext == ".mp3":
                from mutagen.id3 import APIC
                from mutagen.mp3 import MP3
                audio = MP3(str(path))
                if audio.tags is None:
                    audio.add_tags()
                audio.tags.delall("APIC")
                audio.tags.add(APIC(encoding=3, mime=self._mime(image), type=3, desc="Cover", data=image))
                audio.save()
            elif ext in (".m4a", ".mp4"):
                from mutagen.mp4 import MP4, MP4Cover
                audio = MP4(str(path))
                if audio.tags is None:
                    audio.add_tags()
                fmt = MP4Cover.FORMAT_PNG if image[:8] == b"\x89PNG\r\n\x1a\n" else MP4Cover.FORMAT_JPEG
                audio["covr"] = [MP4Cover(image, imageformat=fmt)]
                audio.save()
            elif ext == ".wav":
                from mutagen.id3 import APIC
                from mutagen.wave import WAVE
                audio = WAVE(str(path))
                if audio.tags is None:
                    audio.add_tags()
                audio.tags.add(APIC(encoding=3, mime=self._mime(image), type=3, desc="Cover", data=image))
                audio.save()
        except Exception:
            # 封面嵌入失败不影响播放
            pass

    @staticmethod
    def _mime(image: bytes) -> str:
        return "image/png" if image[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"

    def save_cover_file(self, audio_path: Path, image: bytes) -> None:
        if not image or not self.cfg.write_cover_file:
            return
        target = audio_path.parent / "cover.jpg"
        if target.exists():
            return
        try:
            target.write_bytes(image)
            os.chmod(target, self.FILE_MODE)
        except OSError:
            pass

    def save_lyric(self, audio_path: Path, lyric: str) -> None:
        if not lyric or not self.cfg.write_lyrics_file:
            return
        target = audio_path.parent / (audio_path.stem + ".lrc")
        try:
            target.write_text(lyric, encoding="utf-8")
            os.chmod(target, self.FILE_MODE)
        except OSError:
            pass

    # ---------- 清理 ----------
    @staticmethod
    def remove_track_files(path: Path) -> list[str]:
        """删除音频及其 sidecar, 并清理变空的目录. 返回删除清单."""
        removed: list[str] = []
        for candidate in (path, path.with_suffix(".lrc")):
            try:
                if candidate.exists():
                    candidate.unlink()
                    removed.append(str(candidate))
            except OSError:
                pass
        # 往上清理空目录(最多两层: 专辑、艺人), 不动 MUSIC_DIR 本身
        parent = path.parent
        for _ in range(2):
            try:
                if parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
                else:
                    break
            except OSError:
                break
        return removed
