"""通知分发.

设计要点
--------
1. **为什么不用 Emby webhook**
   Emby 只知道两件事: "库里有新文件" 和 "条目从库里消失了", 而且要等它自己扫描
   (你这边 ScanSchedule=1h)才触发。而我们需要通知的事件里, 收藏增删、取消收藏
   回推、洗版换档、跨平台回退、下载失败原因, Emby 一个都看不到 —— 这些事件
   只有 MuseHub 自己知道。绕一圈 Emby 只会更慢、更少、更容易误报(文件移动会被
   Emby 当成"删除+新增")。
   需要把事件接到别的系统时, 配 `notify.webhook_url`, 我们会 POST 一份 JSON 过去,
   格式见 `_post_webhook`。

2. **为什么要聚合**
   回填阶段一轮可能入库几百首。逐首推 300 条消息既刷屏又会触发 Telegram 限流,
   所以按"一轮同步"为单位聚合: 入库/失败/删除各自合成一条, 超出 max_lines 折叠。

3. **线程安全**
   下载在 ThreadPoolExecutor 里跑, 事件来自多个线程, 所有缓冲都在锁内操作。
"""
from __future__ import annotations

import json
import base64
import hashlib
import hmac
import re
import threading
import time
from html import escape
from typing import Any, Callable
from urllib.parse import quote_plus

try:                      # requests 是项目已有依赖, 这里只做兜底
    import requests
except Exception:         # pragma: no cover
    requests = None       # type: ignore

# 事件类型 -> (开关字段, 中文名)
KINDS: dict[str, tuple[str, str]] = {
    "playlist": ("playlist_changes", "歌单变化"),
    "added": ("track_added", "新歌入库"),
    "removed": ("track_removed", "歌曲删除"),
    "failed": ("download_failed", "下载失败"),
    "dedup": ("dedup", "洗版去重"),
    "error": ("errors", "异常"),
    "summary": ("summary", "同步汇总"),
}

PROVIDER_LABEL = {"netease": "网易云", "qq": "QQ音乐", "local": "本地"}

SEP = "─────────────"


def _fmt_size(n: Any) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return ""


def _fmt_dur(sec: Any) -> str:
    try:
        sec = int(sec)
    except (TypeError, ValueError):
        return ""
    if sec <= 0:
        return ""
    m, s = divmod(sec, 60)
    if m >= 60:
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def parse_quiet(text: str) -> tuple[int, int] | None:
    """'23:30-08:00' -> (1410, 480). 解析不了就当没设置."""
    raw = (text or "").strip().replace("：", ":")
    if "-" not in raw:
        return None
    a, _, b = raw.partition("-")
    try:
        h1, m1 = (int(x) for x in a.strip().split(":"))
        h2, m2 = (int(x) for x in b.strip().split(":"))
    except (ValueError, TypeError):
        return None
    if not (0 <= h1 < 24 and 0 <= h2 < 24 and 0 <= m1 < 60 and 0 <= m2 < 60):
        return None
    return h1 * 60 + m1, h2 * 60 + m2


def in_quiet(text: str, now: float | None = None) -> bool:
    span = parse_quiet(text)
    if not span:
        return False
    lt = time.localtime(now if now is not None else time.time())
    cur = lt.tm_hour * 60 + lt.tm_min
    start, end = span
    if start == end:
        return False
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end          # 跨零点


class _Null:
    """引擎不接通知时用的空实现, 省掉满屏 if self.notify."""

    def __getattr__(self, _name: str):
        def _noop(*_a: Any, **_kw: Any) -> None:
            return None
        return _noop


NULL = _Null()


class Notifier:
    def __init__(self, cfg, db, telegram_send: Callable[[str], None] | None = None,
                 telegram_photo: Callable[[str, str], bool] | None = None):
        self.cfg = cfg
        self.db = db
        self._tg = telegram_send
        self._tg_photo = telegram_photo
        self._lock = threading.Lock()
        self._pending: list[str] = []        # 免打扰期间攒下的消息
        self._webhook_warned = 0.0
        self.begin()

    # ---------------- 生命周期 ----------------
    def begin(self) -> None:
        with self._lock:
            self._added: list[str] = []
            self._failed: list[str] = []
            self._removed: list[str] = []
            self._playlists: list[str] = []
            self._dedup: list[str] = []
            self._started = time.time()

    @property
    def conf(self):
        return getattr(self.cfg, "notify", None)

    def _on(self, kind: str) -> bool:
        conf = self.conf
        if conf is None or not getattr(conf, "enabled", False):
            return False
        field = KINDS.get(kind, ("", ""))[0]
        return bool(getattr(conf, field, True)) if field else True

    # ---------------- 事件入口 ----------------
    def playlist_change(self, name: str, added: int, removed: int) -> None:
        if not self._on("playlist") or (not added and not removed):
            return
        part = []
        if added:
            part.append(f"➕ {added}")
        if removed:
            part.append(f"➖ {removed}")
        with self._lock:
            self._playlists.append(f"• {escape(name)}：{' / '.join(part)}")

    def track_added(self, provider: str, artist: str, title: str,
                    quality: str = "", path: str = "",
                    info: dict | None = None) -> None:
        """info 可带 cover/album/year/duration/size/ext, 用于图片卡片排版."""
        if not self._on("added"):
            return
        item = {"provider": provider, "artist": artist or "", "title": title,
                "quality": quality or "", "path": path or ""}
        if info:
            for k in ("cover", "album", "year", "duration", "size", "ext"):
                if info.get(k):
                    item[k] = info[k]
        label = f"{artist} - {title}".strip(" -") or title
        with self._lock:
            self._added.append(item)
        self.db.log("info", "notify", f"入库通知: {label} [{quality}]", path=path)

    def track_failed(self, provider: str, artist: str, title: str, reason: str) -> None:
        if not self._on("failed"):
            return
        label = f"{artist} - {title}".strip(" -") or title
        with self._lock:
            self._failed.append(f"• {escape(label)} — {escape(str(reason)[:60])}")

    def track_removed(self, why: str, artist: str, title: str, path: str = "") -> None:
        if not self._on("removed"):
            return
        label = f"{artist} - {title}".strip(" -") or title
        with self._lock:
            self._removed.append(f"• {escape(label)} — {escape(why)}")

    def dedup_note(self, text: str) -> None:
        if not self._on("dedup"):
            return
        with self._lock:
            self._dedup.append(f"• {escape(text)}")

    def error(self, source: str, message: str) -> None:
        if not self._on("error"):
            return
        self._send([f"⚠️ <b>{escape(source)}</b>", SEP, escape(str(message)[:300])],
                   kind="error")

    def test(self) -> tuple[bool, str]:
        """设置页「测试」按钮用: 立刻发一条, 绕过免打扰."""
        conf = self.conf
        if conf is None or not getattr(conf, "enabled", False):
            return False, "通知总开关未打开"
        sent = []
        if getattr(conf, "telegram", True) and self._tg is not None:
            self._tg("🔔 <b>MuseHub 通知测试</b>\n" + SEP +
                     "\n🔔 收到这条说明 Telegram 通道正常。")
            sent.append("Telegram")
        if getattr(conf, "webhook_url", ""):
            ok, err = self._post_webhook({"event": "test", "text": "MuseHub 通知测试"})
            if ok:
                sent.append("Webhook")
            else:
                return False, f"Webhook 发送失败: {err}"
        im_sent = self._fanout_im("🔔 MuseHub 通知测试\n\n收到这条说明通道正常。", "test")
        if im_sent:
            sent.extend(im_sent)
        elif getattr(conf, "wecom_url", "") or getattr(conf, "dingtalk_url", "") \
                or getattr(conf, "feishu_url", ""):
            return False, "IM 通道发送失败，请检查 Webhook 地址与密钥"
        if not sent:
            return False, "没有可用通道（Telegram 未启动，且未填 Webhook / 企业微信 / 钉钉 / 飞书）"
        return True, "已发送: " + " + ".join(sent)

    # ---------------- 发送 ----------------
    def _post_webhook(self, payload: dict) -> tuple[bool, str]:
        url = (getattr(self.conf, "webhook_url", "") or "").strip()
        if not url:
            return False, "未配置"
        if requests is None:
            return False, "缺少 requests 依赖"
        body = {"source": "musehub", "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **payload}
        try:
            r = requests.post(url, json=body, timeout=8)
            if r.status_code >= 300:
                return False, f"HTTP {r.status_code}"
            return True, ""
        except Exception as exc:  # noqa: BLE001 - 外部服务不可用不能影响同步
            return False, str(exc)[:120]

    # ---------------- 国内 IM 机器人 ----------------
    # 这些消息原本按 Telegram 的 HTML 排版(<b>/<code>), 其他通道只收纯文本,
    # 所以先剥掉标签再发, 避免出现一堆尖括号。
    _TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")

    def _plain(self, text: str) -> str:
        t = self._TAG_RE.sub("", text or "")
        for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&"),
                     ("&quot;", '"'), ("&#39;", "'")):
            t = t.replace(a, b)
        return re.sub(r"\n{3,}", "\n\n", t).strip()

    def _post_json(self, url: str, body: dict, *, name: str) -> tuple[bool, str]:
        if requests is None:
            return False, "缺少 requests 依赖"
        try:
            r = requests.post(url, json=body, timeout=8)
            if r.status_code >= 300:
                return False, f"HTTP {r.status_code}"
            try:
                data = r.json()
            except ValueError:
                return True, ""
            # 三家都是 200 里带错误码, 必须看返回体
            code = data.get("errcode", data.get("code", 0))
            if code not in (0, None):
                return False, f"{name} 返回 {code}: {str(data.get('errmsg') or data.get('msg') or '')[:80]}"
            return True, ""
        except Exception as exc:  # noqa: BLE001 - 外部服务异常不能影响同步
            return False, str(exc)[:120]

    def _send_wecom(self, text: str) -> tuple[bool, str]:
        url = (getattr(self.conf, "wecom_url", "") or "").strip()
        if not url:
            return False, "未配置"
        return self._post_json(url, {"msgtype": "text", "text": {"content": text[:1800]}},
                               name="企业微信")

    def _send_dingtalk(self, text: str) -> tuple[bool, str]:
        url = (getattr(self.conf, "dingtalk_url", "") or "").strip()
        if not url:
            return False, "未配置"
        secret = (getattr(self.conf, "dingtalk_secret", "") or "").strip()
        if secret:
            ts = str(round(time.time() * 1000))
            digest = hmac.new(secret.encode(), f"{ts}\n{secret}".encode(),
                              digestmod=hashlib.sha256).digest()
            sign = quote_plus(base64.b64encode(digest))
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}timestamp={ts}&sign={sign}"
        return self._post_json(url, {"msgtype": "text", "text": {"content": text[:1800]}},
                               name="钉钉")

    def _send_feishu(self, text: str) -> tuple[bool, str]:
        url = (getattr(self.conf, "feishu_url", "") or "").strip()
        if not url:
            return False, "未配置"
        body: dict = {"msg_type": "text", "content": {"text": text[:1800]}}
        secret = (getattr(self.conf, "feishu_secret", "") or "").strip()
        if secret:
            ts = str(int(time.time()))
            digest = hmac.new(f"{ts}\n{secret}".encode(), digestmod=hashlib.sha256).digest()
            body["timestamp"] = ts
            body["sign"] = base64.b64encode(digest).decode()
        return self._post_json(url, body, name="飞书")

    def _fanout_im(self, text: str, kind: str = "info") -> list[str]:
        """把一条消息发给所有已配置的 IM 通道, 返回成功的通道名."""
        plain = self._plain(text)
        sent: list[str] = []
        for sender, label in ((self._send_wecom, "企业微信"),
                              (self._send_dingtalk, "钉钉"),
                              (self._send_feishu, "飞书")):
            if not getattr(self.conf, sender.__name__.replace("_send_", "") + "_url", ""):
                continue
            ok, err = sender(plain)
            if ok:
                sent.append(label)
            elif time.time() - self._webhook_warned > 600:
                self._webhook_warned = time.time()
                self.db.log("warn", "notify", f"{label} 推送失败: {err}")
        return sent

    def _send(self, lines: list[str], kind: str = "info", force: bool = False,
              photo: str | None = None) -> None:
        if not lines:
            return
        conf = self.conf
        if conf is None or not getattr(conf, "enabled", False):
            return
        text = "\n".join(lines)
        quiet = getattr(conf, "quiet_hours", "") or ""
        if not force and in_quiet(quiet):
            with self._lock:
                self._pending.append(text)
                if len(self._pending) > 30:      # 防止离线/免打扰太久把内存撑爆
                    self._pending = self._pending[-30:]
            return
        self._flush_pending()
        if getattr(conf, "telegram", True) and self._tg is not None:
            # 有图先试 sendPhoto(带排版好的 caption), 失败退回纯文本, 通知不丢
            sent = False
            if photo and self._tg_photo is not None:
                try:
                    sent = self._tg_photo(photo, text)
                except Exception as exc:  # noqa: BLE001
                    self.db.log("warn", "notify", f"图片通知异常: {exc}")
            if not sent:
                try:
                    self._tg(text)
                except Exception as exc:  # noqa: BLE001
                    self.db.log("warn", "notify", f"Telegram 推送失败: {exc}")
        if getattr(conf, "webhook_url", ""):
            ok, err = self._post_webhook({"event": kind, "text": text})
            if not ok and time.time() - self._webhook_warned > 600:
                self._webhook_warned = time.time()
                self.db.log("warn", "notify", f"Webhook 推送失败: {err}")
        self._fanout_im(text, kind)

    def _flush_pending(self) -> None:
        with self._lock:
            if not self._pending:
                return
            batch, self._pending = self._pending, []
        conf = self.conf
        head = f"🔕 <b>免打扰期间积压 {len(batch)} 条</b>"
        body = "\n\n".join(batch)
        if getattr(conf, "telegram", True) and self._tg is not None:
            try:
                self._tg(head + "\n\n" + body)
            except Exception:  # noqa: BLE001
                pass
        self._fanout_im(head + "\n\n" + body, "pending")

    def _bullets(self, title: str, items: list[str]) -> list[str]:
        conf = self.conf
        limit = max(1, int(getattr(conf, "max_lines", 15) or 15))
        shown = items[:limit]
        out = [f"{title}（{len(items)}）", SEP] + shown
        if len(items) > limit:
            out.append(f"…还有 {len(items) - limit} 条")
        return out

    # ---------------- 单曲卡片排版 ----------------
    def _track_caption(self, t: dict) -> str:
        from .settings import quality_label
        q = quality_label(t.get("provider", ""), t.get("quality", "")) or t.get("quality") or ""
        ext = (t.get("ext") or "").upper()
        lines = ["🎵 <b>新歌入库</b>", SEP, f"<b>{escape(t.get('title') or '未知歌名')}</b>"]
        if t.get("album"):
            album = f"💿 {escape(str(t['album']))}"
            if t.get("year"):
                album += f"（{escape(str(t['year']))}）"
            lines.append(album)
        lines.append(f"👤 {escape(t.get('artist') or '未知艺人')}")
        meta = [x for x in (ext, escape(q)) if x]
        if meta:
            lines.append("🎼 " + " · ".join(meta))
        size = _fmt_size(t.get("size"))
        if size:
            lines.append(f"📦 {size}")
        dur = _fmt_dur(t.get("duration"))
        if dur:
            lines.append(f"⏱ {dur}")
        lines.append(f"🏷 {PROVIDER_LABEL.get(t.get('provider'), t.get('provider') or '未知')}")
        return "\n".join(lines)

    def _track_line(self, t: dict) -> str:
        label = f"{t.get('artist')} - {t.get('title')}".strip(" -") or t.get("title") or "?"
        tags = []
        ext = (t.get("ext") or "").upper()
        if ext:
            tags.append(ext)
        if t.get("quality"):
            tags.append(escape(str(t["quality"])))
        tail = f" [{ ' / '.join(tags)}]" if tags else ""
        return f"• {escape(label)}{tail}"

    def flush_events(self) -> None:
        """把攒下的分类事件发出去(不含汇总). 手动操作后可以立刻调用."""
        with self._lock:
            added, failed = self._added[:], self._failed[:]
            removed, dedup = self._removed[:], self._dedup[:]
            playlists = self._playlists[:]
        self.begin()

        if playlists:
            self._send(["📃 <b>歌单变化</b>", SEP] + playlists, kind="playlist")

        if removed:
            self._send(self._bullets("🗑 <b>歌曲删除</b>", removed), kind="removed")

        if added:
            conf = self.conf
            verbose = bool(getattr(conf, "verbose_tracks", True))
            if not verbose:
                self._send([f"🎵 <b>新歌入库 {len(added)} 首</b>"], kind="added")
            elif len(added) <= 3:
                # 少量入库: 每首一张"封面 + 信息卡", 对齐 Symedia 的影视入库通知
                for t in added:
                    self._send([self._track_caption(t)], kind="added",
                               photo=t.get("cover") or None)
            else:
                self._send(self._bullets("🎵 <b>新歌入库</b>",
                                         [self._track_line(t) for t in added]),
                           kind="added")

        if failed:
            self._send(self._bullets("⚠️ <b>下载失败</b>", failed), kind="failed")

        if dedup:
            self._send(self._bullets("✨ <b>洗版/去重</b>", dedup), kind="dedup")

    def finish(self, stats: dict) -> None:
        """一轮同步收尾: 把攒下的事件发出去, 再补一条汇总."""
        self.flush_events()

        if not self._on("summary"):
            return
        if stats.get("dry_run") or stats.get("skipped"):
            return
        if stats.get("error"):
            self._send(["❌ <b>同步失败</b>", SEP, escape(str(stats["error"])[:300])],
                       kind="error")
            return
        bits = []
        for label, key in (("🎵 入库", "downloaded"), ("⚠️ 失败", "download_failed"),
                           ("🗑 删除", "deleted"), ("↩️ 取消收藏", "actions_done")):
            if stats.get(key):
                bits.append(f"{label} {stats[key]}")
        if stats.get("missing"):
            bits.append(f"❓ 本地缺失 {stats['missing']}")
        if stats.get("dedup_reused"):
            bits.append(f"♻️ 去重复用 {stats['dedup_reused']}")
        if not bits:
            return                      # 空转的一轮不打扰
        elapsed = stats.get("elapsed", "?")
        self._send(["✅ <b>同步完成</b> · " + time.strftime("%H:%M"), SEP,
                    " · ".join(bits), f"⏱ 耗时 {elapsed} 秒"], kind="summary")
