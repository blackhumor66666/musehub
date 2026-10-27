"""运行时容器: 把配置、数据库、平台、引擎装在一起, 并负责后台调度.

为什么不用 APScheduler:
  我们只需要"每 N 分钟跑一次 + 手动触发一次", 一个带 Event 的线程就够,
  依赖更少, 出问题时栈更浅, 也不会出现调度器把主线程拖死的情况.
"""
from __future__ import annotations

import json
import threading
import time
import traceback
from typing import Any

from .config import Config
from .db import Database
from .downloader import Downloader
from .navidrome import Navidrome
from .notify import Notifier
from .organizer import Organizer
from .progress import ProgressTracker
from .providers.registry import make_providers
from .sync import SyncEngine


class Context:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.db = Database(cfg.db_path)
        # 面板里保存过的配置优先于 .env, 必须在建 provider 之前套用
        from . import settings as settings_mod
        self.loaded_settings = settings_mod.load_into(cfg, self.db)

        self.providers = make_providers(cfg, self.db)
        self.navidrome = Navidrome(cfg.navidrome, self.db)
        self.organizer = Organizer(cfg.organizer)
        self.downloader = Downloader(cfg, self.db, self.providers)
        self.progress = ProgressTracker()
        self.notifier = Notifier(cfg, self.db, telegram_send=self._send_telegram,
                                 telegram_photo=self._send_telegram_photo)
        self.engine = SyncEngine(cfg, self.db, self.providers, self.downloader,
                                 self.organizer, self.navidrome, progress=self.progress,
                                 notify=self.notifier)
        self.refresh_nd_accounts()
        self.bot: Any = None
        self._sync_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._next_full = time.time() + 15
        self._next_reconcile = time.time() + 60
        self._next_cookie_check = time.time() + 25
        self.started_at = time.time()

    # ---------- 同步触发 ----------
    def refresh_nd_accounts(self) -> dict:
        """加载额外播放器账号(用户名->密码), 供"按账号分歌单"使用."""
        raw = self.db.kv_get("navidrome_accounts", "") or ""
        accounts: dict[str, str] = {}
        try:
            for item in json.loads(raw or "[]"):
                u = str(item.get("username") or "").strip()
                p = str(item.get("password") or "")
                if u and p:
                    accounts[u] = p
        except (ValueError, TypeError, AttributeError):
            self.db.log("warn", "navidrome", "播放器账号配置格式错误, 已忽略")
        self.engine.nd_accounts = accounts
        return accounts

    def _send_telegram(self, text: str) -> None:
        """通知通道: 机器人没起来时静默跳过(还有 Webhook 兜底)."""
        bot = self.bot
        if bot is not None:
            bot._notify(text)

    def _send_telegram_photo(self, photo_url: str, caption: str) -> bool:
        """图片通知: 给每个管理员发 sendPhoto, 任一成功即算成功.

        失败返回 False, 调用方会退回纯文本, 保证通知不丢。
        """
        bot = self.bot
        if bot is None:
            return False
        sent = False
        for admin in self.cfg.telegram.admin_ids:
            try:
                bot.api.send_photo(admin, photo_url, caption)
                sent = True
            except Exception as exc:  # noqa: BLE001 - 图发不出去不算大事
                self.db.log("warn", "notify", f"图片通知失败: {exc}")
        return sent

    def trigger_sync(self, trigger: str = "manual", fetch_remote: bool = True) -> bool:
        """立即返回, 同步在后台线程跑. 已在跑则返回 False."""
        if self._sync_thread and self._sync_thread.is_alive():
            return False
        def _run() -> None:
            try:
                self.engine.run(trigger=trigger, fetch_remote=fetch_remote)
            except Exception as exc:  # noqa: BLE001
                self.db.log("error", "runtime", f"同步线程异常: {exc}\n{traceback.format_exc()[:1500]}")
        self._sync_thread = threading.Thread(target=_run, name="musehub-sync", daemon=True)
        self._sync_thread.start()
        return True

    def provider_list(self) -> dict:
        """去重后的 {实例键: provider} —— 第一账号的别名与自身是同一对象, 只保留一个."""
        out: dict = {}
        seen: set[int] = set()
        for key, p in self.providers.items():
            if id(p) in seen:
                continue
            seen.add(id(p))
            out[key] = p
        return out

    def sync_running(self) -> bool:
        return bool(self._sync_thread and self._sync_thread.is_alive())

    # ---------- Cookie 体检 ----------
    COOKIE_ALERT_GAP = 12 * 3600   # 持续失效时, 每 12 小时提醒一次(避免刷屏)

    def cookie_health(self) -> dict[str, dict]:
        """上次体检结果(持久化在 kv, 重启后仍能判断"是从正常变成失效")."""
        try:
            raw = self.db.kv_get("cookie_health", "") or ""
            return json.loads(raw) if raw else {}
        except Exception:  # noqa: BLE001
            return {}

    def check_cookies(self, alert: bool = True) -> dict[str, dict]:
        """逐个平台检查登录态. 失效立刻告警, 之后每 12 小时提醒一次, 恢复也告知."""
        from .db import now_iso

        prev_all = self.cookie_health()
        out: dict[str, dict] = {}
        now_ts = time.time()
        for name, p in self.provider_list().items():
            try:
                ok, msg = p.auth_check()
            except Exception as exc:  # noqa: BLE001 - 单个平台异常不能影响其他
                ok, msg = False, f"检查异常: {str(exc)[:120]}"
            prev = prev_all.get(name) or {}
            still_bad = prev.get("ok") is False and not ok
            entry = {
                "ok": bool(ok),
                "message": msg,
                "checked_at": now_iso(),
                "since": prev.get("since") if still_bad else now_iso(),
                "alerted_at": prev.get("alerted_at") or "",
            }
            if alert and not ok:
                last_ts = 0.0
                try:
                    last_ts = float(prev.get("alerted_at") or 0)
                except (TypeError, ValueError):
                    last_ts = 0.0
                # 首次失效立刻提醒; 之后每隔 COOKIE_ALERT_GAP 再提醒一次
                if prev.get("ok") is not False or (now_ts - last_ts) > self.COOKIE_ALERT_GAP:
                    self.notifier.error(
                        p.display,
                        f"登录已失效：{msg}\n"
                        f"影响：该平台的歌单同步 / 搜索下载 / 收藏回写都会失败。\n"
                        f"处理：面板「设置 → 平台账号」重新粘贴 Cookie。",
                    )
                    entry["alerted_at"] = str(now_ts)
                    self.db.log("warn", "cookie", f"{p.display} Cookie 失效: {msg}")
            elif alert and prev.get("ok") is False and ok:
                self.notifier.error(p.display, "登录已恢复，同步可以正常跑了。")
                self.db.log("info", "cookie", f"{p.display} Cookie 已恢复")
            out[name] = entry
        self.db.kv_set("cookie_health", json.dumps(out, ensure_ascii=False))
        return out

    def wait_sync(self, timeout: float = 600) -> bool:
        if self._sync_thread:
            self._sync_thread.join(timeout)
            return not self._sync_thread.is_alive()
        return True

    # ---------- 调度 ----------
    def start_scheduler(self) -> None:
        threading.Thread(target=self._schedule_loop, name="musehub-scheduler", daemon=True).start()

    def _backlog(self) -> int:
        """还有多少首在等下载(决定要不要加速回填)."""
        try:
            return int(self.db.scalar(
                """SELECT COUNT(*) FROM tracks t
                   WHERE t.status IN ('pending','failed') AND t.attempts < 3
                     AND EXISTS (SELECT 1 FROM playlist_items pi
                                 JOIN playlists p ON p.id=pi.playlist_id
                                 WHERE pi.track_id=t.id AND p.subscribed=1)""", default=0))
        except Exception:  # noqa: BLE001
            return 0

    def reschedule(self) -> None:
        """配置改了周期后, 按新周期重新排下一次."""
        now = time.time()
        self._next_full = now + max(5, self.cfg.sync.interval_minutes) * 60
        self._next_reconcile = now + max(2, self.cfg.sync.reconcile_interval_minutes) * 60

    def _schedule_loop(self) -> None:
        self.db.log("info", "runtime", "调度器已启动",
                    full_interval_min=self.cfg.sync.interval_minutes,
                    reconcile_interval_min=self.cfg.sync.reconcile_interval_minutes)
        while not self._stop.wait(5):
            now = time.time()
            if now >= self._next_cookie_check:
                self._next_cookie_check = now + 30 * 60
                try:
                    self.check_cookies()
                except Exception as exc:  # noqa: BLE001 - 体检失败不能拖垮调度
                    self.db.log("warn", "runtime", f"Cookie 体检异常: {exc}")
            if now >= self._next_full:
                # 回填模式: 还有大量待下载时, 3 分钟后继续, 下完再回到常规周期
                backlog = self._backlog()
                gap = 180 if backlog > 0 else max(5, self.cfg.sync.interval_minutes) * 60
                self._next_full = now + gap
                if backlog:
                    self.db.log("info", "runtime",
                                f"回填模式: 还有 {backlog} 首待下载, {gap // 60} 分钟后继续")
                if not self.sync_running():
                    self.trigger_sync("schedule")
            if now >= self._next_reconcile:
                self._next_reconcile = now + max(2, self.cfg.sync.reconcile_interval_minutes) * 60
                if not self.sync_running():
                    self.trigger_sync("reconcile", fetch_remote=False)

    # ---------- Telegram 生命周期 ----------
    def start_bot(self) -> tuple[bool, str]:
        if not self.cfg.telegram.token:
            return False, "未配置 Bot Token"
        if self.bot is not None:
            return True, "已在运行"
        try:
            from .tg.bot import Bot
            self.bot = Bot(self)
            self.bot.start()
            return True, "已启动"
        except Exception as exc:  # noqa: BLE001
            self.bot = None
            return False, str(exc)

    def stop_bot(self) -> None:
        if self.bot is not None:
            try:
                self.bot.stop()
            except Exception:  # noqa: BLE001
                pass
            self.bot = None

    def restart_bot(self) -> tuple[bool, str]:
        self.stop_bot()
        return self.start_bot()

    def stop(self) -> None:
        self._stop.set()
        self.stop_bot()

    # ---------- 状态 ----------
    def status(self) -> dict[str, Any]:
        stats = self.db.stats()
        return {
            "version": "0.1.0",
            "uptime_seconds": int(time.time() - self.started_at),
            "syncing": self.sync_running(),
            "dry_run": self.engine.dry_run,
            "delete_policy": self.engine.delete_policy,
            "capabilities": self.cfg.capability_report(),
            "providers": {name: p.status() for name, p in self.provider_list().items()},
            "stats": stats,
            "music_dir": str(self.cfg.music_dir),
            "next_interval_minutes": self.cfg.sync.interval_minutes,
        }
