"""Telegram 机器人 (精简版).

和原版最大的区别: 任何一条命令都不做重活.
  * 同步、下载全部丢到后台线程, 命令本身 100ms 内返回
  * 轮询循环每轮独立 try/except, 单条消息出错不会让机器人整体卡死
  * offset 落库, 重启不重复处理旧消息
  * 网络异常指数退避, 最长 2 分钟, 不会疯狂重试被限流

命令:
  /status /sync /playlists /add /rm /search /queue /logs /mode /selftest /help
"""
from __future__ import annotations

import threading
import time
import traceback

from ..providers.base import RemoteTrack
from ..providers.registry import resolve_playlist_input
from .api import TgApi, TgError, chunks, esc, keyboard, strip_html

HELP = """<b>MuseHub 指令</b>

/menu        按钮菜单（推荐，点着用）
/status      运行状态与统计
/sync        立即同步一次(后台执行)
/playlists   查看订阅的歌单
/add &lt;链接&gt;  订阅网易云/QQ歌单
/rm &lt;序号&gt;    取消订阅
/search &lt;词&gt; 搜索并单曲下载
/missing     还差哪些歌没下 / 一键补
/queue       查看待回推动作
/logs        最近事件
/mode        切换 试运行/实盘
/selftest    平台连通性自检
"""

# 主菜单按钮 —— 手机端不用记命令
MENU_ROWS = [
    [("📊 状态", "menu|status"), ("▶️ 立即同步", "menu|sync")],
    [("📋 歌单", "menu|playlists"), ("📥 待补下载", "menu|missing")],
    [("🧪 切模式", "menu|mode"), ("🔌 平台自检", "menu|selftest")],
]

# "这首歌属于某个已订阅歌单" —— 待补统计反复用到
SUBSCRIBED_EXISTS = """EXISTS (SELECT 1 FROM playlist_items pi
                               JOIN playlists p ON p.id=pi.playlist_id
                               WHERE pi.track_id=t.id AND p.subscribed=1)"""


class Bot:
    def __init__(self, ctx):
        self.ctx = ctx
        self.cfg = ctx.cfg
        self.api = TgApi(self.cfg.telegram.token, self.cfg.telegram.proxy)
        self._offset = int(ctx.db.kv_get("tg_offset", "0") or 0)
        self._stop = threading.Event()
        self._cache: dict[str, RemoteTrack] = {}
        self._cache_lock = threading.Lock()
        self._last_err_notify = 0.0

    # ---------- 生命周期 ----------
    def start(self) -> None:
        threading.Thread(target=self._run, name="musehub-bot", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            me = self.api.me()
            self.ctx.db.log("info", "telegram", f"机器人已上线 @{me.get('username')}")
        except TgError as exc:
            self.ctx.db.log("error", "telegram", f"机器人启动失败(检查 token/代理): {exc}")
            return
        backoff = 5
        fails = 0
        while not self._stop.is_set():
            try:
                updates = self.api.get_updates(self._offset, self.cfg.telegram.poll_timeout)
                backoff = 5
                fails = 0
                for upd in updates:
                    uid = int(upd.get("update_id") or 0)
                    if uid >= self._offset:
                        self._offset = uid + 1
                        self.ctx.db.kv_set("tg_offset", str(self._offset))
                    self._dispatch(upd)
            except TgError as exc:
                fails += 1
                # 日志降噪: 前 3 次 + 每 20 次记一条, 否则长期断网会把日志刷满
                if fails <= 3 or fails % 20 == 0:
                    self.ctx.db.log("warn", "telegram",
                                    f"轮询异常(连续第 {fails} 次): {exc}")
                # 偶发抖动(代理重启/切节点)不打扰用户; 连续 3 次失败才提醒, 30 分钟最多一次
                if fails >= 3 and time.time() - self._last_err_notify > 1800:
                    self._last_err_notify = time.time()
                    proxy = self.cfg.telegram.proxy or "未配置代理"
                    self._notify(
                        f"⚠️ Telegram 连续 {fails} 次连不上（会自动重试，恢复后不用管）\n"
                        f"原因示例: {esc(str(exc)[:120])}\n"
                        f"当前代理: <code>{esc(proxy)}</code>\n"
                        f"提示: 代理所在设备关机/切节点时会断, 长时间不恢复就检查一下它")
                self._stop.wait(backoff)
                # 上限 10 分钟: 代理关机这种长期故障不该每 2 分钟撞一次
                backoff = min(backoff * 2, 600)
            except Exception as exc:  # noqa: BLE001
                self.ctx.db.log("error", "telegram",
                                f"轮询未捕获异常: {exc}\n{traceback.format_exc()[:800]}")
                self._stop.wait(5)

    # ---------- 分发 ----------
    def _dispatch(self, update: dict) -> None:
        try:
            if "message" in update:
                self._on_message(update["message"])
            elif "callback_query" in update:
                self._on_callback(update["callback_query"])
        except Exception as exc:  # noqa: BLE001
            self.ctx.db.log("error", "telegram", f"处理消息失败: {exc}\n{traceback.format_exc()[:800]}")
            chat_id = self._chat_of(update)
            if chat_id:
                self._safe_send(chat_id, f"❌ 处理出错: {esc(str(exc)[:300])}")

    @staticmethod
    def _chat_of(update: dict) -> int | None:
        msg = update.get("message") or {}
        cb = update.get("callback_query") or {}
        return (msg.get("chat") or {}).get("id") or ((cb.get("message") or {}).get("chat") or {}).get("id")

    def _authorized(self, user_id: int) -> bool:
        admins = self.cfg.telegram.admin_ids
        return not admins or user_id in admins

    def _safe_send(self, chat_id: int, text: str, markup: dict | None = None) -> None:
        parts = chunks(text)
        for i, part in enumerate(parts):
            mk = markup if i == len(parts) - 1 else None
            try:
                self.api.send(chat_id, part, markup=mk)
            except TgError as exc:
                # 关键: HTML 转义漏了会让整条消息 400 发不出去, 用户看到的就是
                # "点了按钮没反应"。这里降级成纯文本重发, 内容至少能到。
                if "parse entities" in str(exc) or "can't parse" in str(exc):
                    try:
                        self.api.send(chat_id, strip_html(part), markup=mk, html=False)
                        self.ctx.db.log("warn", "telegram",
                                        f"HTML 解析失败已降级为纯文本: {str(exc)[:80]}")
                        continue
                    except TgError:
                        pass
                self.ctx.db.log("warn", "telegram", f"发送失败: {exc}")
                return

    def _notify(self, text: str) -> None:
        for admin in self.cfg.telegram.admin_ids:
            self._safe_send(admin, text)

    def notify_sync(self, stats: dict) -> None:
        """每轮同步结束后的汇总推送."""
        if not self.cfg.telegram.admin_ids:
            return
        if stats.get("dry_run") or stats.get("skipped"):
            return
        interesting = any(stats.get(k) for k in
                          ("downloaded", "download_failed", "deleted", "actions_done",
                           "missing", "playlist_errors"))
        if not interesting:
            return
        if stats.get("error"):
            self._notify(f"❌ 同步失败: {esc(str(stats['error'])[:200])}")
            return
        lines = ["🎵 <b>同步完成</b>",
                 f"下载 {stats.get('downloaded', 0)}"
                 f"（失败 {stats.get('download_failed', 0)}）",
                 f"本地缺失 {stats.get('missing', 0)} · 清理 {stats.get('deleted', 0)}"
                 f" · 取消收藏 {stats.get('actions_done', 0)}"]
        if stats.get("orphans_cleaned"):
            lines.append(f"清理孤儿记录 {stats['orphans_cleaned']}")
        if stats.get("playlist_errors"):
            lines.append(f"⚠️ 歌单拉取异常 {stats['playlist_errors']} 个")
        lines.append(f"耗时 {stats.get('elapsed', '?')}s")
        self._notify("\n".join(lines))

    # ---------- 消息 ----------
    def _on_message(self, msg: dict) -> None:
        chat_id = (msg.get("chat") or {}).get("id")
        user_id = (msg.get("from") or {}).get("id") or 0
        text = (msg.get("text") or "").strip()
        if not chat_id or not text:
            return
        if not self._authorized(user_id):
            self._safe_send(chat_id, "⛔️ 无权限. 请把 ADMIN_USER_ID 设成你的 Telegram ID.")
            return
        if not text.startswith("/"):
            self._safe_send(chat_id, "发 /help 看指令. 直接贴歌单链接也可以.")
            return

        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        handler = {
            "/start": self._cmd_help, "/help": self._cmd_help, "/menu": self._cmd_menu,
            "/status": self._cmd_status, "/sync": self._cmd_sync,
            "/playlists": self._cmd_playlists, "/add": self._cmd_add,
            "/rm": self._cmd_rm, "/search": self._cmd_search,
            "/missing": self._cmd_missing,
            "/queue": self._cmd_queue, "/logs": self._cmd_logs,
            "/mode": self._cmd_mode, "/selftest": self._cmd_selftest,
        }.get(cmd)
        if handler is None:
            self._safe_send(chat_id, "未知指令, /help 查看可用命令")
            return
        handler(chat_id, arg)

    # ---------- 命令 ----------
    def _cmd_help(self, chat_id: int, _arg: str) -> None:
        self._safe_send(chat_id, HELP, markup=keyboard(MENU_ROWS))

    def _cmd_menu(self, chat_id: int, _arg: str) -> None:
        self._safe_send(chat_id, "👇 点按钮操作", markup=keyboard(MENU_ROWS))

    def _cmd_status(self, chat_id: int, _arg: str) -> None:
        st = self.ctx.status()
        s = st["stats"]
        last = s.get("last_run") or {}
        lines = [
            "<b>🎧 MuseHub</b>",
            f"模式: {'🧪 试运行' if st['dry_run'] else '🚀 实盘'} / 删除策略: {st['delete_policy']}",
            f"同步中: {'是' if st['syncing'] else '否'}",
            "",
            f"曲目 活跃 {s['tracks_active']} / 缺失 {s['tracks_missing']} / 失败 {s['tracks_failed']}",
            f"订阅歌单: {s['playlists']}",
            f"待回推: {s['actions_pending']} / 失败: {s['actions_failed']}",
            "",
            "平台: " + " | ".join(f"{k}: {v}" for k, v in st["providers"].items()),
        ]
        if last:
            lines += ["", f"上次同步: {last.get('started_at', '')} → {last.get('state', '')}"]
        self._safe_send(chat_id, "\n".join(lines))

    def _cmd_sync(self, chat_id: int, _arg: str) -> None:
        if self.ctx.trigger_sync("telegram"):
            self._safe_send(chat_id, "▶️ 同步已开始, 完成后会通知你.")
        else:
            self._safe_send(chat_id, "⏳ 已经在同步了, 稍等.")

    def _cmd_playlists(self, chat_id: int, _arg: str) -> None:
        rows = self.ctx.db.query("SELECT * FROM playlists ORDER BY id")
        if not rows:
            self._safe_send(chat_id, "还没有订阅任何歌单. 用 /add &lt;链接&gt; 添加.")
            return
        lines = ["<b>📚 歌单</b>"]
        for r in rows:
            flag = "✅" if r["subscribed"] else "⏸"
            lines.append(f"{flag} <code>{r['id']}</code> [{r['provider']}] {esc(r['name'])} "
                         f"({r['track_count']})")
        self._safe_send(chat_id, "\n".join(lines) + "\n\n/rm &lt;序号&gt; 取消订阅")

    def _cmd_add(self, chat_id: int, arg: str) -> None:
        resolved = resolve_playlist_input(arg)
        if not resolved:
            self._safe_send(chat_id, "❌ 没认出链接. 支持网易云/QQ音乐的歌单、专辑、榜单链接"
                                     "或纯数字 ID.")
            return
        provider_name, pid, kind = resolved
        provider = self.ctx.providers.get(provider_name)
        if provider is None:
            self._safe_send(chat_id, f"❌ 平台 {provider_name} 未启用")
            return
        label = {"album": "专辑", "chart": "榜单"}.get(kind, "歌单")
        self._safe_send(chat_id, f"🔍 正在读取{label}…")

        def work() -> None:
            try:
                pl = provider.album(pid) if kind == "album" else (
                    provider.chart(pid) if kind == "chart" else provider.playlist(pid))
            except Exception as exc:  # noqa: BLE001
                self._safe_send(chat_id, f"❌ 读取失败: {esc(str(exc)[:300])}")
                return
            row_id = self.ctx.db.upsert_playlist(provider_name, pid, pl.name, pl.cover,
                                                 len(pl.tracks), kind=kind)
            self.ctx.db.execute("UPDATE playlists SET subscribed=1 WHERE id=?", (row_id,))
            self.ctx.db.log("info", "telegram", f"订阅{label} {pl.name} ({len(pl.tracks)} 首)")
            self._safe_send(chat_id, f"✅ 已订阅{label} <b>{esc(pl.name)}</b>, 共 {len(pl.tracks)} 首\n"
                                     f"后台开始同步, 完成后通知你.")
            self.ctx.trigger_sync("telegram-add")
        threading.Thread(target=work, daemon=True).start()

    def _cmd_rm(self, chat_id: int, arg: str) -> None:
        if not arg.isdigit():
            self._safe_send(chat_id, "用法: /rm &lt;歌单序号&gt;")
            return
        row = self.ctx.db.get_playlist(int(arg))
        if not row:
            self._safe_send(chat_id, "❌ 没有这个序号")
            return
        self.ctx.db.execute("UPDATE playlists SET subscribed=0 WHERE id=?", (row["id"],))
        self._safe_send(chat_id, f"⏸ 已取消订阅 <b>{esc(row['name'])}</b>\n"
                                 f"注意: 已下载的文件不会被删除(除非你手动清理).")

    def _cmd_search(self, chat_id: int, arg: str) -> None:
        if not arg:
            self._safe_send(chat_id, "用法: /search 歌名 歌手")
            return

        def work() -> None:
            lines = [f"<b>🔎 {esc(arg)}</b>"]
            buttons: list[list[tuple[str, str]]] = []
            idx = 1
            for name, provider in self.ctx.providers.items():
                try:
                    hits = provider.search(arg, limit=4)
                except Exception as exc:  # noqa: BLE001
                    lines.append(f"{provider.display}: 搜索失败 {esc(str(exc)[:80])}")
                    continue
                for t in hits:
                    token = f"{name}:{t.remote_id}"
                    with self._cache_lock:
                        self._cache[token] = t
                    lines.append(f"{idx}. [{provider.display}] {esc(t.title)} — {esc(t.artist_str)}"
                                 f" <i>{esc(t.album or '单曲')}</i>")
                    buttons.append([(f"⬇️ {idx}", f"dl|{token}")])
                    idx += 1
            if idx == 1:
                self._safe_send(chat_id, "\n".join(lines) + "\n没找到结果.")
                return
            self._safe_send(chat_id, "\n".join(lines), markup=keyboard(buttons))
        threading.Thread(target=work, daemon=True).start()

    def _cmd_missing(self, chat_id: int, _arg: str) -> None:
        """查库补全: 订阅歌单里还有哪些没下到, 可一键重置重试."""
        def work() -> None:
            db = self.ctx.db
            got = {r["status"]: int(r["n"]) for r in db.query(
                f"""SELECT t.status, COUNT(*) AS n FROM tracks t
                    WHERE t.status IN ('pending','failed') AND {SUBSCRIBED_EXISTS}
                    GROUP BY t.status""")}
            pending, failed = got.get("pending", 0), got.get("failed", 0)
            stuck = int(db.scalar(
                f"""SELECT COUNT(*) FROM tracks t
                    WHERE t.status='failed' AND t.attempts>=3 AND {SUBSCRIBED_EXISTS}""",
                default=0))
            lines = ["<b>📥 待补下载</b>",
                     f"排队中: {pending}",
                     f"失败: {failed}" + (f"（其中 {stuck} 首已放弃自动重试）" if stuck else "")]
            if not pending and not failed:
                lines.append("✅ 订阅歌单已经全部下载完成")
                self._safe_send(chat_id, "\n".join(lines))
                return
            sample = db.query(
                f"""SELECT t.title, t.artists, t.fail_reason FROM tracks t
                    WHERE t.status='failed' AND {SUBSCRIBED_EXISTS}
                    ORDER BY t.updated_at DESC LIMIT 5""")
            if sample:
                lines.append("")
                for r in sample:
                    reason = (r["fail_reason"] or "")[:40]
                    lines.append(f"· {esc(r['title'])} — {esc(r['artists'])}"
                                 + (f" <i>{esc(reason)}</i>" if reason else ""))
            self._safe_send(chat_id, "\n".join(lines), markup=keyboard([
                [("🔄 重置失败并重试", "miss|retry")],
                [("▶️ 立即同步", "menu|sync")],
            ]))
        threading.Thread(target=work, daemon=True).start()

    def _cmd_queue(self, chat_id: int, _arg: str) -> None:
        acts = self.ctx.db.pending_actions(20)
        if not acts:
            self._safe_send(chat_id, "✅ 没有待回推的动作")
            return
        lines = ["<b>📤 待回推</b>"]
        for a in acts:
            title = a["payload"].get("title", "") if isinstance(a["payload"], dict) else ""
            lines.append(f"<code>{a['id']}</code> {a['state']} {a['kind']} "
                         f"[{a['provider']}] {esc(title)} {esc(a['last_error'][:60])}")
        self._safe_send(chat_id, "\n".join(lines))

    def _cmd_logs(self, chat_id: int, _arg: str) -> None:
        events = self.ctx.db.recent_events(15)
        if not events:
            self._safe_send(chat_id, "暂无日志")
            return
        icon = {"info": "·", "warn": "⚠️", "error": "❌"}
        lines = ["<b>📝 最近事件</b>"]
        for e in events:
            lines.append(f"{icon.get(e['level'], '·')} <code>{e['ts'][11:19]}</code> "
                         f"[{e['source']}] {esc(e['message'][:120])}")
        self._safe_send(chat_id, "\n".join(lines))

    def _cmd_mode(self, chat_id: int, arg: str) -> None:
        if arg in ("dry", "live"):
            self.ctx.db.kv_set("dry_run", "1" if arg == "dry" else "0")
            self._safe_send(chat_id, "🧪 已切到试运行" if arg == "dry" else "🚀 已切到实盘")
            return
        cur = self.ctx.engine.dry_run
        self._safe_send(
            chat_id,
            f"当前: {'🧪 试运行' if cur else '🚀 实盘'}\n选择模式:",
            markup=keyboard([[("🧪 试运行", "mode|dry"), ("🚀 实盘", "mode|live")]]),
        )

    def _cmd_selftest(self, chat_id: int, _arg: str) -> None:
        def work() -> None:
            lines = ["<b>🩺 自检</b>"]
            for name, p in self.ctx.providers.items():
                lines.append(f"{p.display}: {esc(p.status())}")
                try:
                    hits = p.search("test", limit=1)
                    lines.append(f"  搜索: {'ok' if hits else '无结果'}")
                except Exception as exc:  # noqa: BLE001
                    lines.append(f"  搜索: ❌ {esc(str(exc)[:80])}")
            if self.ctx.navidrome.available():
                probe = self.ctx.navidrome.probe()
                lines.append(f"Navidrome native: {'ok' if probe['native'] else '❌'} / "
                             f"subsonic: {'ok' if probe['subsonic'] else '❌'}")
            else:
                lines.append("Navidrome: 未配置")
            self._safe_send(chat_id, "\n".join(lines))
        threading.Thread(target=work, daemon=True).start()

    # ---------- 回调 ----------
    def _on_callback(self, cb: dict) -> None:
        user_id = (cb.get("from") or {}).get("id") or 0
        if not self._authorized(user_id):
            self.api.answer_callback(cb.get("id", ""), "无权限", alert=True)
            return
        data = cb.get("data") or ""
        msg = cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        message_id = msg.get("message_id")
        cb_id = cb.get("id", "")

        if data.startswith("menu|"):
            self.api.answer_callback(cb_id, "…")
            action = data[5:]
            {"status": self._cmd_status, "sync": self._cmd_sync,
             "playlists": self._cmd_playlists, "missing": self._cmd_missing,
             "mode": self._cmd_mode, "selftest": self._cmd_selftest,
             }.get(action, lambda *_: None)(chat_id, "")
            return

        if data == "miss|retry":
            def _retry() -> None:
                cur = self.ctx.db.execute(
                    "UPDATE tracks SET attempts=0 WHERE status='failed' AND attempts>=3")
                n = cur.rowcount or 0
                self.ctx.db.log("info", "telegram", f"手动重置 {n} 首失败曲目的重试次数")
                started = self.ctx.trigger_sync("telegram")
                self._safe_send(chat_id,
                                f"🔄 已重置 {n} 首失败曲目\n"
                                + ("▶️ 同步已开始, 完成后通知你"
                                   if started else "⏳ 已有同步在跑, 下一轮会自动补下"))
            self.api.answer_callback(cb_id, "已提交")
            threading.Thread(target=_retry, daemon=True).start()
            return

        if data.startswith("dl|"):
            token = data[3:]
            with self._cache_lock:
                track = self._cache.get(token)
            if not track:
                self.api.answer_callback(cb_id, "结果已过期, 重新 /search", alert=True)
                return
            self.api.answer_callback(cb_id, "开始下载…")
            self._download_async(chat_id, message_id, track)
            return

        if data.startswith("mode|"):
            mode = data.split("|", 1)[1]
            self.ctx.db.kv_set("dry_run", "1" if mode == "dry" else "0")
            self.api.answer_callback(cb_id, "已切换")
            if chat_id and message_id:
                try:
                    self.api.edit(chat_id, message_id,
                                  f"已切换为 {'🧪 试运行' if mode == 'dry' else '🚀 实盘'}")
                except TgError:
                    pass
            return

        self.api.answer_callback(cb_id, "未知操作")

    def _download_async(self, chat_id: int, message_id: int | None, track: RemoteTrack) -> None:
        def work() -> None:
            try:
                res = self.ctx.engine.download_single(track)
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "error": str(exc)}
            text = (f"✅ 已下载 <b>{esc(track.title)}</b>" if res.get("ok")
                    else f"❌ 下载失败: {esc(str(res.get('error'))[:300])}")
            try:
                if chat_id and message_id:
                    self.api.edit(chat_id, message_id, text)
                elif chat_id:
                    self._safe_send(chat_id, text)
            except TgError:
                pass
        threading.Thread(target=work, daemon=True).start()
