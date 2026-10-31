"""Web 管理面板 (FastAPI).

只做四件事: 看状态、触发同步、管订阅、处理失败动作.
认证: 配置了 WEB_PASSWORD 就启用, 支持 Cookie(面板)与 Basic(脚本)两种方式.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .. import settings as settings_mod
from ..db import now_iso
from ..providers.registry import resolve_playlist_input
from ..runtime import Context

security = HTTPBasic(auto_error=False)
TEMPLATE = Path(__file__).parent / "templates" / "index.html"
COOKIE = "mh_key"


def _key_of(password: str) -> str:
    return hashlib.sha256(("musehub:" + password).encode()).hexdigest()[:32]


def create_app(ctx: Context) -> FastAPI:
    app = FastAPI(title="MuseHub", docs_url=None, redoc_url=None)

    # 曲库统计要聚合几千条记录, 面板每 5 秒轮询一次太浪费 -> 缓存 60 秒
    _lib_cache: dict = {"at": 0.0, "data": None}

    def library_stats(force: bool = False) -> dict:
        now = time.time()
        if force or _lib_cache["data"] is None or now - _lib_cache["at"] > 60:
            _lib_cache["data"] = ctx.db.library_stats()
            _lib_cache["at"] = now
        return _lib_cache["data"]

    def auth(request: Request, creds: HTTPBasicCredentials | None = Depends(security)) -> None:
        password = ctx.cfg.web_password
        if not password:
            return
        if request.cookies.get(COOKIE) == _key_of(password):
            return
        if creds is not None and \
           secrets.compare_digest(creds.username, ctx.cfg.web_username) and \
           secrets.compare_digest(creds.password, password):
            return
        # 千万不要在这里返回 WWW-Authenticate: Basic —— 浏览器会弹原生认证框并
        # 把页面后续所有请求挂起, 表现就是"前端打不开 / 一直加载中"。
        # 我们自己有 Cookie 登录框, 用自定义头告诉前端该登录了。
        raise HTTPException(status_code=401, headers={"X-MuseHub-Login": "required"})

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        # 页面本身不含数据, 公开; 数据接口全部走 auth.
        # 支持 /?key=密码 一次性种 Cookie, 方便书签/无头场景
        resp = HTMLResponse(TEMPLATE.read_text(encoding="utf-8"),
                            headers={"Cache-Control": "no-store, max-age=0"})
        key = request.query_params.get("key")
        if key and ctx.cfg.web_password and secrets.compare_digest(key, ctx.cfg.web_password):
            resp.set_cookie(COOKIE, _key_of(ctx.cfg.web_password), httponly=True,
                            samesite="lax", max_age=30 * 86400)
        return resp

    @app.post("/api/login")
    async def login(request: Request) -> JSONResponse:
        body = await request.json()
        password = ctx.cfg.web_password
        if not password:
            return JSONResponse({"ok": True})
        given = str(body.get("password") or "")
        if not secrets.compare_digest(given, password):
            return JSONResponse({"ok": False, "error": "密码错误"}, status_code=401)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, _key_of(password), httponly=True,
                        samesite="lax", max_age=30 * 86400)
        return resp

    @app.get("/api/status")
    def status(_: None = Depends(auth)) -> JSONResponse:
        data = ctx.status()
        # 不再附带 30 条日志(13KB): 日志页走 /api/logs, 那里支持筛选与分页
        data["actions"] = ctx.db.pending_actions(30)
        data["playlist_rows"] = [
            dict(r) for r in ctx.db.query(
                """SELECT p.*,
                          (SELECT COUNT(*) FROM playlist_items pi WHERE pi.playlist_id=p.id) AS items,
                          (SELECT COUNT(*) FROM tracks t JOIN playlist_items pi ON pi.track_id=t.id
                            WHERE pi.playlist_id=p.id AND t.status='active') AS downloaded
                   FROM playlists p ORDER BY p.id"""
            )
        ]
        data["last_run_stats"] = ctx.db.query_one(
            "SELECT stats FROM sync_runs WHERE state='done' ORDER BY id DESC LIMIT 1"
        )["stats"] if ctx.db.scalar("SELECT COUNT(*) FROM sync_runs WHERE state='done'", default=0) else "{}"
        data["telegram"] = {
            "configured": bool(ctx.cfg.telegram.token),
            "running": ctx.bot is not None,
        }
        data["password_set"] = bool(ctx.cfg.web_password)
        data["vip"] = {name: p.vip_status() for name, p in ctx.provider_list().items()}
        return JSONResponse(data)

    @app.post("/api/sync")
    def sync(_: None = Depends(auth)) -> JSONResponse:
        started = ctx.trigger_sync("web")
        return JSONResponse({"started": started})

    @app.post("/api/navidrome/sync")
    def navidrome_sync(_: None = Depends(auth)) -> JSONResponse:
        """手动把歌单镜像到 Navidrome(不等整轮同步)."""
        if not ctx.navidrome.available():
            return JSONResponse({"ok": False, "error": "还没配置 Navidrome 地址/账号"},
                                status_code=400)
        if ctx.engine.dry_run:
            return JSONResponse({"ok": False, "error": "试运行模式下不会写播放器"}, status_code=400)

        def _run() -> None:
            try:
                stats = ctx.engine.mirror_player()
                ctx.db.log("info", "navidrome",
                           f"手动镜像完成: 歌单 {stats.get('navidrome_playlists', 0)} 个"
                           f", 曲目未收录 {stats.get('navidrome_missing', 0)}",
                           **{k: v for k, v in stats.items() if isinstance(v, (int, str))})
            except Exception as exc:  # noqa: BLE001
                ctx.db.log("warn", "navidrome", f"手动镜像失败: {exc}")

        threading.Thread(target=_run, name="navidrome-mirror", daemon=True).start()
        return JSONResponse({"ok": True, "started": True})

    @app.post("/api/mode")
    async def mode(request: Request, _: None = Depends(auth)) -> JSONResponse:
        body = await request.json()
        if "dry_run" in body:
            ctx.db.kv_set("dry_run", "1" if body["dry_run"] else "0")
        if "delete_policy" in body and body["delete_policy"] in ("never", "managed_only", "mirror"):
            ctx.db.kv_set("delete_policy", body["delete_policy"])
        return JSONResponse({"dry_run": ctx.engine.dry_run, "delete_policy": ctx.engine.delete_policy})

    @app.post("/api/playlists")
    async def add_playlist(request: Request, _: None = Depends(auth)) -> JSONResponse:
        body = await request.json()
        raw = (body.get("input") or "").strip()
        resolved = resolve_playlist_input(raw)
        if not resolved:
            return JSONResponse({"ok": False, "error": "无法识别的链接或 ID"}, status_code=400)
        provider_name, pid, kind = resolved
        # 多账号: 逐个账号尝试读取(歌单可见性因账号而异), 谁能读就用谁
        keys = sorted([k for k in ctx.providers if k.split(":")[0] == provider_name],
                      key=lambda k: (k != provider_name,))
        pl, used_key = None, ""
        last_err = ""
        for key in keys:
            try:
                p = ctx.providers[key]
                pl = p.album(pid) if kind == "album" else (
                    p.chart(pid) if kind == "chart" else p.playlist(pid))
                used_key = key
                break
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)[:200]
        if pl is None:
            label = {"album": "专辑", "chart": "榜单"}.get(kind, "歌单")
            return JSONResponse({"ok": False,
                                 "error": last_err or f"所有账号都无法读取该{label}"},
                                status_code=400)
        account = used_key.split(":", 1)[1] if ":" in used_key else ""
        provider = ctx.providers[used_key]
        row_id = ctx.db.upsert_playlist(provider_name, pid, pl.name, pl.cover, len(pl.tracks),
                                        account=account, kind=kind)
        ctx.db.execute("UPDATE playlists SET subscribed=1 WHERE id=?", (row_id,))
        # 订阅到『我喜欢』类歌单时, 自动把它登记为收藏歌单(收藏双向同步用)
        if provider_name == "qq" and "喜欢" in (pl.name or ""):
            qq = ctx.providers.get(used_key)
            if qq is not None:
                qq._fav_id = str(pid)          # type: ignore[attr-defined]
                qq._fav_checked = True         # type: ignore[attr-defined]
            ctx.db.log("info", "web", f"已自动识别 QQ『我喜欢』歌单 id={pid} (账号: {account or '默认'})")
        ctx.db.log("info", "web", f"订阅歌单 {pl.name} ({len(pl.tracks)} 首)")
        ctx.trigger_sync("web-add")
        return JSONResponse({"ok": True, "id": row_id, "name": pl.name, "tracks": len(pl.tracks)})

    @app.post("/api/playlists/{row_id}/toggle")
    def toggle_playlist(row_id: int, _: None = Depends(auth)) -> JSONResponse:
        row = ctx.db.get_playlist(row_id)
        if not row:
            raise HTTPException(404, "歌单不存在")
        new_state = 0 if row["subscribed"] else 1
        ctx.db.execute("UPDATE playlists SET subscribed=? WHERE id=?", (new_state, row_id))
        return JSONResponse({"subscribed": bool(new_state)})

    @app.delete("/api/playlists/{row_id}")
    def drop_playlist(row_id: int, _: None = Depends(auth)) -> JSONResponse:
        row = ctx.db.get_playlist(row_id)
        if not row:
            raise HTTPException(404, "歌单不存在")
        ctx.db.drop_playlist(row_id)
        # 同步删掉播放器里的镜像歌单, 否则它会永远留在那(用户视角就是"删了还在")
        removed = False
        if row["navidrome_id"] and ctx.navidrome.available():
            try:
                ctx.navidrome.delete_playlist(row["navidrome_id"])
                removed = True
            except Exception as exc:  # noqa: BLE001 - 播放器删失败不影响本地删除
                ctx.db.log("warn", "navidrome",
                           f"删除镜像歌单失败 [{row['navidrome_name']}]: {exc}")
        return JSONResponse({"ok": True, "mirror_removed": removed})

    @app.post("/api/playlists/{row_id}/rename")
    async def rename_playlist(row_id: int, request: Request,
                              _: None = Depends(auth)) -> JSONResponse:
        """改名: 只改播放器里显示的名字, 不动平台上的歌单."""
        row = ctx.db.get_playlist(row_id)
        if not row:
            raise HTTPException(404, "歌单不存在")
        body = await request.json()
        display = (body.get("display_name") or "").strip()
        ctx.db.execute("UPDATE playlists SET display_name=? WHERE id=?", (display, row_id))
        label = ctx.engine._provider_label(row["provider"])
        acct = (row.get("account") or "").strip()
        if acct and acct != "默认":
            label = f"{label}·{acct}"
        new_name = f"{ctx.cfg.navidrome.playlist_prefix}[{label}] {display or row['name']}"
        renamed = False
        if row["navidrome_id"] and ctx.navidrome.available():
            try:
                ctx.navidrome.rename_playlist(row["navidrome_id"], new_name)
                ctx.db.execute("UPDATE playlists SET navidrome_name=? WHERE id=?",
                               (new_name, row_id))
                renamed = True
            except Exception as exc:  # noqa: BLE001
                ctx.db.log("warn", "navidrome", f"播放器改名失败 [{new_name}]: {exc}")
        return JSONResponse({"ok": True, "display_name": display,
                             "navidrome_name": new_name, "renamed": renamed})

    # ---------- 播放器多账号(私有歌单只有 owner 看得见) ----------
    @app.get("/api/ndaccounts")
    def ndaccounts(_: None = Depends(auth)) -> JSONResponse:
        raw = ctx.db.kv_get("navidrome_accounts", "") or ""
        try:
            items = json.loads(raw or "[]")
        except ValueError:
            items = []
        return JSONResponse({
            "default": ctx.cfg.navidrome.username,
            "accounts": [{"username": i.get("username", ""),
                          "configured": bool(i.get("password"))} for i in items],
        })

    @app.post("/api/ndaccounts")
    async def save_ndaccounts(request: Request, _: None = Depends(auth)) -> JSONResponse:
        body = await request.json()
        items = []
        for a in (body.get("accounts") or []):
            u = str(a.get("username") or "").strip()
            p = str(a.get("password") or "")
            if u and p:
                items.append({"username": u, "password": p})
        ctx.db.kv_set("navidrome_accounts", json.dumps(items, ensure_ascii=False))
        ctx.refresh_nd_accounts()
        # 顺便验证每个账号能不能登录; 401 时区分"没这个用户"和"密码不对"
        known = set(ctx.navidrome.list_users()) if ctx.navidrome.available() else set()
        results = []
        for it in items:
            ok, msg = True, ""
            try:
                client = ctx.navidrome.for_account(it["username"], it["password"])
                client._login(force=True)
            except Exception as exc:  # noqa: BLE001
                ok, msg = False, str(exc)[:100]
                if "401" in msg:
                    if known and it["username"] not in known:
                        msg = (f"Navidrome 里没有用户 {it['username']}，"
                               f"请先在 Navidrome 后台 → 用户 → 新建用户")
                    else:
                        msg = "用户名或密码不对（Navidrome 返回 401），请核对密码"
            results.append({"username": it["username"], "ok": ok, "message": msg})
        return JSONResponse({"ok": True, "results": results})

    @app.post("/api/ndaccounts/create")
    async def create_ndaccount(request: Request, _: None = Depends(auth)) -> JSONResponse:
        """直接在 MuseHub 里创建 Navidrome 用户(用 admin 权限), 建完自动登记."""
        body = await request.json()
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        if not username or not password:
            raise HTTPException(400, "用户名和密码都要填")
        if not ctx.navidrome.available():
            raise HTTPException(400, "还没配置 Navidrome 地址")
        created = True
        try:
            ctx.navidrome.create_user(username, password)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            # 用户名已存在(Navidrome 返回 ra.validation.unique) -> 当作"绑定已有用户":
            # 只要密码对, 直接登记即可, 不用让用户先去后台删号重建
            if "unique" in msg or "400" in msg:
                try:
                    ctx.navidrome.for_account(username, password)._login(force=True)
                except Exception:  # noqa: BLE001
                    raise HTTPException(
                        400, f"用户 {username} 已存在于 Navidrome，但密码不对。"
                             f"请填对该用户的密码；想换密码请去 Navidrome 后台改。")
                created = False
            else:
                raise HTTPException(400, msg[:200])
        # 登记到账号列表并验证能登录
        raw = ctx.db.kv_get("navidrome_accounts", "") or "[]"
        try:
            items = json.loads(raw)
        except ValueError:
            items = []
        if not any(i.get("username") == username for i in items):
            items.append({"username": username, "password": password})
            ctx.db.kv_set("navidrome_accounts", json.dumps(items, ensure_ascii=False))
        ctx.refresh_nd_accounts()
        ok, msg = True, ""
        try:
            ctx.navidrome.for_account(username, password)._login(force=True)
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, str(exc)[:120]
        return JSONResponse({"ok": True, "username": username, "verified": ok,
                             "created": created, "message": msg})

    @app.post("/api/playlists/{row_id}/mirror-account")
    async def set_mirror_account(row_id: int, request: Request,
                                 _: None = Depends(auth)) -> JSONResponse:
        """把歌单镜像给一个或多个播放器账号(逗号分隔); 移除的账号会删掉其镜像."""
        row = ctx.db.get_playlist(row_id)
        if not row:
            raise HTTPException(404, "歌单不存在")
        body = await request.json()
        raw = body.get("accounts", body.get("account", ""))
        if isinstance(raw, list):
            new_accounts = [str(x).strip() for x in raw if str(x).strip()]
        else:
            new_accounts = [x.strip() for x in str(raw or "").split(",") if x.strip()]
        old_accounts = [x.strip() for x in (row.get("mirror_account") or "").split(",") if x.strip()]
        target = ",".join(new_accounts)

        # 被移除的账号: 删掉它名下的镜像, 否则会留孤儿歌单
        for gone in set(old_accounts) - set(new_accounts):
            mid = ctx.db.query_one(
                "SELECT navidrome_id FROM playlist_mirrors WHERE playlist_id=? AND account=?",
                (row_id, gone))
            pid = (mid["navidrome_id"] if mid else (row["navidrome_id"] or ""))
            if not pid:
                continue
            pwd = (ctx.engine.nd_accounts or {}).get(gone)
            try:
                if gone and gone != ctx.cfg.navidrome.username and pwd:
                    ctx.navidrome.for_account(gone, pwd).delete_playlist(pid)
                elif not gone:
                    ctx.navidrome.delete_playlist(pid)
            except Exception as exc:  # noqa: BLE001
                ctx.db.log("warn", "navidrome", f"删除旧镜像失败: {exc}")
            ctx.db.execute(
                "DELETE FROM playlist_mirrors WHERE playlist_id=? AND account=?", (row_id, gone))

        ctx.db.execute(
            "UPDATE playlists SET mirror_account=?, navidrome_id='', navidrome_name='' WHERE id=?",
            (target, row_id))
        return JSONResponse({"ok": True, "accounts": new_accounts,
                             "moved": target != ",".join(old_accounts)})

    @app.get("/api/dashboard")
    def dashboard(_: None = Depends(auth)) -> JSONResponse:
        db = ctx.db
        by_status = [
            {"status": r["status"], "count": int(r["c"])}
            for r in db.query(
                "SELECT status, COUNT(*) AS c FROM tracks WHERE status<>'deleted' GROUP BY status"
            )
        ]
        by_platform = [
            {"provider": r["provider"], "count": int(r["c"])}
            for r in db.query(
                """SELECT t.provider, COUNT(*) AS c FROM tracks t
                   JOIN playlist_items pi ON pi.track_id=t.id
                   JOIN playlists p ON p.id=pi.playlist_id AND p.subscribed=1
                   WHERE t.status='active' GROUP BY t.provider"""
            )
        ]
        recent_syncs = []
        for pl in db.query(
            "SELECT * FROM playlists WHERE subscribed=1 ORDER BY last_sync_at DESC LIMIT 6"
        ):
            downloaded = int(db.scalar(
                """SELECT COUNT(*) FROM tracks t JOIN playlist_items pi ON pi.track_id=t.id
                   WHERE pi.playlist_id=? AND t.status='active'""",
                (pl["id"],), 0))
            total = int(pl["track_count"] or 0)
            recent_syncs.append({
                "id": pl["id"], "name": pl["name"], "provider": pl["provider"],
                "cover": pl["cover"], "total": total, "downloaded": downloaded,
                "rate": round(downloaded * 100.0 / total, 1) if total else None,
                "synced_at": pl["last_sync_at"],
                "error": pl["last_error"],
            })
        first_day = (db.scalar("SELECT MIN(created_at) FROM tracks") or "")[:10]
        recent_uploads = [dict(r) for r in db.query(
            """SELECT provider, title, artists, quality, ext, size, updated_at
               FROM tracks WHERE status='active' AND managed=1
               ORDER BY updated_at DESC LIMIT 8"""
        )]
        return JSONResponse({
            "providers": {name: p.status() for name, p in ctx.provider_list().items()},
            "vip": {name: p.vip_status() for name, p in ctx.provider_list().items()},
            "stats": db.stats(),
            "library": library_stats(),
            "tracks_by_status": by_status,
            "tracks_by_platform": by_platform,
            "recent_syncs": recent_syncs,
            "recent_uploads": recent_uploads,
            "download_summary": db.download_summary(),
            "cookie_health": ctx.cookie_health(),
            "library_age_days": None if not first_day else first_day,
            "checklist": {
                "netease_cookie": bool(ctx.cfg.netease.cookie),
                "qq_cookie": bool(ctx.cfg.qq.cookie),
                "any_playlist": db.scalar("SELECT COUNT(*) FROM playlists WHERE subscribed=1", default=0) > 0,
                "dry_run": ctx.engine.dry_run,
            },
        })

    @app.post("/api/cookies/check")
    def cookies_check(_: None = Depends(auth)) -> JSONResponse:
        """立刻体检一次登录态(设置页/仪表盘的手动检查按钮)."""
        return JSONResponse({"ok": True, "health": ctx.check_cookies()})

    @app.get("/api/library")
    def library(_: None = Depends(auth)) -> JSONResponse:
        return JSONResponse(library_stats(force=True))

    @app.get("/api/downloads")
    def downloads(provider: str = "", status: str = "", page: int = 1, size: int = 30,
                  _: None = Depends(auth)) -> JSONResponse:
        data = ctx.db.download_history(provider=provider, status=status, page=page, size=size)
        data["summary"] = ctx.db.download_summary()
        data["live"] = ctx.progress.snapshot()
        return JSONResponse(data)

    @app.get("/api/playlists/{row_id}/tracks")
    def playlist_tracks(row_id: int, _: None = Depends(auth)) -> JSONResponse:
        pl = ctx.db.get_playlist(row_id)
        if not pl:
            raise HTTPException(404, "歌单不存在")
        rows = ctx.db.query(
            """SELECT t.id, t.provider, t.remote_id, t.title, t.artists, t.album,
                      t.duration, t.status, t.quality, t.ext, t.size, t.path, t.fail_reason
               FROM tracks t JOIN playlist_items pi ON pi.track_id=t.id
               WHERE pi.playlist_id=? ORDER BY pi.position""",
            (row_id,),
        )
        return JSONResponse({"playlist": pl, "tracks": [dict(r) for r in rows]})

    @app.get("/api/logs")
    def logs(level: str = "", source: str = "", q: str = "",
             page: int = 1, size: int = 60,
             _: None = Depends(auth)) -> JSONResponse:
        db = ctx.db
        conds, params = [], []
        if level:
            conds.append("level=?")
            params.append(level)
        if source:
            conds.append("source=?")
            params.append(source)
        if q.strip():
            conds.append("(message LIKE ? OR source LIKE ?)")
            like = f"%{q.strip()}%"
            params.extend([like, like])
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        total = int(db.scalar(f"SELECT COUNT(*) FROM events {where}", params, 0))
        rows = db.query(
            f"SELECT * FROM events {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, min(200, size), max(0, page - 1) * size),
        )
        sources = [r["source"] for r in db.query(
            "SELECT source, COUNT(*) n FROM events GROUP BY source ORDER BY n DESC LIMIT 20")]
        return JSONResponse({"total": total, "page": page, "size": min(200, size),
                             "items": [dict(r) for r in rows], "sources": sources})

    @app.post("/api/downloads/retry-failed")
    def retry_failed(_: None = Depends(auth)) -> JSONResponse:
        cur = ctx.db.execute(
            "UPDATE tracks SET status='pending', attempts=0, fail_reason='' WHERE status='failed'"
        )
        ctx.trigger_sync("web-retry")
        return JSONResponse({"ok": True, "reset": cur.rowcount})

    @app.post("/api/actions/{action_id}/retry")
    def retry_action(action_id: int, _: None = Depends(auth)) -> JSONResponse:
        ctx.db.execute(
            "UPDATE actions SET state='queued', next_at=0, attempts=0, last_error='' WHERE id=?",
            (action_id,),
        )
        return JSONResponse({"ok": True})

    @app.post("/api/actions/{action_id}/skip")
    def skip_action(action_id: int, _: None = Depends(auth)) -> JSONResponse:
        ctx.db.execute("UPDATE actions SET state='skipped' WHERE id=?", (action_id,))
        return JSONResponse({"ok": True})

    @app.get("/api/proxy/cover")
    def cover_proxy(url: str, _: None = Depends(auth)) -> Response:
        """封面图代理: 绕开平台图床的防盗链."""
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").lower()
        if not any(host.endswith(d) for d in ("126.net", "163.com", "qq.com", "qqmusic.qq.com",
                                              "gtimg.cn", "kugou.com", "kuwo.cn", "migu.cn")):
            raise HTTPException(400, "不支持的图片域名")
        import requests as _rq

        try:
            resp = _rq.get(url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://y.qq.com/"})
            if resp.status_code != 200:
                raise HTTPException(404)
            return Response(content=resp.content, media_type=resp.headers.get(
                "Content-Type", "image/jpeg"),
                headers={"Cache-Control": "public, max-age=604800"})
        except _rq.RequestException as exc:
            raise HTTPException(502, f"拉取封面失败: {exc}")

    @app.get("/api/duplicates")
    def duplicates(_: None = Depends(auth)) -> JSONResponse:
        return JSONResponse({"groups": ctx.engine.find_duplicates()})

    @app.post("/api/duplicates/delete")
    async def delete_duplicates(request: Request, _: None = Depends(auth)) -> JSONResponse:
        """处理单组重复: keep=first 保留最早 / keep=best 保留最佳音质(洗版)."""
        body = await request.json()
        gi = body.get("group")
        stats = ctx.engine.dedupe_apply(keep=str(body.get("keep") or "first"),
                                        group_index=int(gi) if gi is not None else None)
        return JSONResponse({"ok": True, **stats})

    # ---------- 元数据刮削: 按平台 ID 补年份/音轨号/专辑/封面 ----------
    _scrape: dict = {"running": False, "mode": "", "progress": {}, "error": "", "result": None}
    _scrape_cache: dict = {"scan": None, "run": None}

    @app.get("/api/scrape/scan")
    def scrape_scan(_: None = Depends(auth)) -> JSONResponse:
        """只读体检(纯查库, 很快)."""
        from .. import scrape as scrape_mod
        res = scrape_mod.scan(ctx.db)
        _scrape_cache["scan"] = res
        return JSONResponse(res)

    @app.get("/api/scrape/status")
    def scrape_status(_: None = Depends(auth)) -> JSONResponse:
        cached = _scrape_cache["scan"] or {}
        # 只回摘要: 全量清单有 5000+ 条(600KB), 前端需要明细时单独调 /api/scrape/scan
        summary = {k: cached[k] for k in ("total", "need", "exact", "search_only",
                                          "counts", "scanned_at") if k in cached}
        return JSONResponse({**_scrape, "cached": summary,
                             "last_run": _scrape_cache["run"]})

    @app.post("/api/scrape/run")
    async def scrape_run(request: Request, _: None = Depends(auth)) -> JSONResponse:
        """补全元数据. body: {fields?: [...], ids?: [...], dry_run?: bool}"""
        if _scrape["running"]:
            return JSONResponse({"ok": False, "error": "已有任务在进行中"}, status_code=409)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        fields = body.get("fields") or None
        ids = body.get("ids")
        dry_run = bool(body.get("dry_run"))
        _scrape.update(running=True, mode="run", error="", result=None,
                       progress={"done": 0, "total": 0, "current": "准备中…"})

        def _run() -> None:
            try:
                from .. import scrape as scrape_mod
                res = scrape_mod.run(ctx, ids=ids, fields=fields or scrape_mod.FIELDS,
                                     write_files=not dry_run, progress=_scrape["progress"],
                                     dry_run=dry_run)
                _scrape["result"] = res
                _scrape_cache["run"] = res
                _scrape_cache["scan"] = scrape_mod.scan(ctx.db)
            except Exception as exc:  # noqa: BLE001
                _scrape["error"] = str(exc)[:300]
            finally:
                _scrape["running"] = False
                _scrape["progress"] = {}

        threading.Thread(target=_run, name="mh-scrape", daemon=True).start()
        return JSONResponse({"ok": True, "started": True})

    # ---------- 库体检: 缺封面 / 缺歌词 ----------
    # 扫全库要逐个打开音频文件读标签, 2500 首要几十秒, 所以同样放后台 + 轮询
    _repair: dict = {"running": False, "mode": "", "progress": {}, "error": "", "result": None}
    _repair_cache: dict = {"scan": None, "fix": None}

    @app.post("/api/repair/scan")
    def repair_scan(_: None = Depends(auth)) -> JSONResponse:
        if _repair["running"]:
            return JSONResponse({"ok": False, "error": "已有任务在进行中"}, status_code=409)
        _repair.update(running=True, mode="scan", error="", result=None,
                       progress={"done": 0, "total": 0, "current": "扫描全库中…"})

        def _run() -> None:
            try:
                from .. import repair as repair_mod
                res = repair_mod.scan(ctx.db, ctx.cfg)
                _repair["result"] = res
                _repair_cache["scan"] = res
            except Exception as exc:  # noqa: BLE001
                _repair["error"] = str(exc)[:300]
            finally:
                _repair["running"] = False
                _repair["progress"] = {}

        threading.Thread(target=_run, name="mh-repair-scan", daemon=True).start()
        return JSONResponse({"ok": True, "started": True})

    @app.get("/api/repair/status")
    def repair_status(_: None = Depends(auth)) -> JSONResponse:
        return JSONResponse({**_repair, "cached": _repair_cache["scan"],
                             "last_fix": _repair_cache["fix"],
                             "ignored": ctx.db.ignored_count()})

    @app.post("/api/repair/ignore")
    async def repair_ignore(request: Request, _: None = Depends(auth)) -> JSONResponse:
        """忽略补不了的条目, 之后检测不再提示. kinds: cover/lyric/meta."""
        body = await request.json()
        ids = [int(i) for i in (body.get("ids") or [])]
        kinds = [k for k in (body.get("kinds") or ["cover", "lyric"])
                 if k in ("cover", "lyric", "meta")]
        if not ids or not kinds:
            raise HTTPException(400, "缺少 ids 或 kinds")
        for k in kinds:
            ctx.db.ignore_items(k, ids)
        _repair_cache["scan"] = None      # 让前端重新扫描时拿到干净结果
        return JSONResponse({"ok": True, "ignored": ctx.db.ignored_count()})

    @app.post("/api/repair/unignore")
    def repair_unignore(kind: str = "", _: None = Depends(auth)) -> JSONResponse:
        """取消忽略; kind 为空表示全部恢复."""
        n = ctx.db.clear_ignored(kind)
        _repair_cache["scan"] = None
        return JSONResponse({"ok": True, "cleared": n})

    @app.post("/api/repair/fix")
    async def repair_fix(request: Request, _: None = Depends(auth)) -> JSONResponse:
        """补封面/歌词. 不传 ids 就是"全库所有缺的"."""
        if _repair["running"]:
            return JSONResponse({"ok": False, "error": "已有任务在进行中"}, status_code=409)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        ids = body.get("ids")
        kinds = body.get("kinds") or ["cover", "lyric"]
        _repair.update(running=True, mode="fix", error="", result=None,
                       progress={"done": 0, "total": 0, "current": "准备中…"})

        def _run() -> None:
            try:
                from .. import repair as repair_mod
                res = repair_mod.fix(ctx, ids=ids, kinds=kinds, progress=_repair["progress"])
                _repair["result"] = res
                _repair_cache["fix"] = res
            except Exception as exc:  # noqa: BLE001
                _repair["error"] = str(exc)[:300]
            finally:
                _repair["running"] = False
                _repair["progress"] = {}

        threading.Thread(target=_run, name="mh-repair-fix", daemon=True).start()
        return JSONResponse({"ok": True, "started": True})


    # ---------- 音质洗版: 有损清单 + 一键升级 ----------
    _qual: dict = {"running": False, "progress": {}, "error": "", "result": None}

    @app.get("/api/blocked")
    def blocked_list(_: None = Depends(auth)) -> JSONResponse:
        rows = [dict(r) for r in ctx.db.query(
            "SELECT provider, remote_id, title, reason, created_at "
            "FROM blocked_tracks ORDER BY created_at DESC")]
        return JSONResponse({"total": len(rows), "items": rows})

    @app.delete("/api/blocked")
    def blocked_clear(_: None = Depends(auth)) -> JSONResponse:
        """清空删除黑名单: 之后这些歌会被重新同步下载."""
        return JSONResponse({"ok": True, "cleared": ctx.db.unblock_all()})

    @app.get("/api/quality/lossy")
    def quality_lossy(_: None = Depends(auth)) -> JSONResponse:
        from ..quality import label as qlabel
        rows = ctx.engine.lossy_tracks()
        by: dict[str, int] = {}
        for r in rows:
            by[r["quality"]] = by.get(r["quality"], 0) + 1
        return JSONResponse({
            "total": len(rows), "ignored": len(ctx.db.ignored_ids().get("quality", set())),
            "by_quality": [{"quality": q, "label": qlabel(q), "count": n}
                           for q, n in sorted(by.items(), key=lambda x: -x[1])],
            "items": [{"id": r["id"], "title": r["title"], "artists": r["artists"],
                       "provider": r["provider"],
                       "quality": r["quality"], "quality_label": qlabel(r["quality"]),
                       "ext": r["ext"], "path": r["path"]} for r in rows],
        })

    @app.post("/api/quality/ignore")
    async def quality_ignore(request: Request, _: None = Depends(auth)) -> JSONResponse:
        body = await request.json()
        ids = [int(i) for i in (body.get("ids") or [])]
        if not ids:
            raise HTTPException(400, "缺少 ids")
        ctx.db.ignore_items("quality", ids)
        return JSONResponse({"ok": True,
                             "ignored": len(ctx.db.ignored_ids().get("quality", set()))})

    @app.post("/api/quality/unignore")
    def quality_unignore(_: None = Depends(auth)) -> JSONResponse:
        ctx.db.clear_ignored("quality")
        return JSONResponse({"ok": True})

    @app.get("/api/quality/status")
    def quality_status(_: None = Depends(auth)) -> JSONResponse:
        return JSONResponse(_qual)

    @app.post("/api/quality/upgrade")
    async def quality_upgrade(request: Request, _: None = Depends(auth)) -> JSONResponse:
        """洗版: 把有损曲目重新按最高档下载, 只替换真的更好的."""
        if _qual["running"]:
            return JSONResponse({"ok": False, "error": "洗版正在进行中"}, status_code=409)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        ids = body.get("ids")
        target = str(body.get("quality") or "lossless").lower()
        _qual.update(running=True, error="", result=None,
                     progress={"done": 0, "total": 0, "current": "准备中…"})

        def _run() -> None:
            try:
                _qual["result"] = ctx.engine.upgrade_quality(
                    ids=ids, progress=_qual["progress"], target=target)
            except Exception as exc:  # noqa: BLE001
                _qual["error"] = str(exc)[:300]
            finally:
                _qual["running"] = False
                _qual["progress"] = {}

        threading.Thread(target=_run, name="mh-quality-upgrade", daemon=True).start()
        return JSONResponse({"ok": True, "started": True})

    # 一键洗版要读几千个文件的真实参数, 放后台跑, 前端轮询进度

    _upg: dict = {"running": False, "stats": None, "error": ""}

    @app.post("/api/duplicates/upgrade-all")
    def upgrade_all_duplicates(_: None = Depends(auth)) -> JSONResponse:
        """一键洗版: 所有重复组保留最佳音质, 差文件进回收目录, 收藏不受影响."""
        if _upg["running"]:
            return JSONResponse({"ok": False, "error": "洗版正在进行中"}, status_code=409)
        _upg.update(running=True, stats=None, error="")

        def _run() -> None:
            try:
                _upg["stats"] = ctx.engine.dedupe_apply(keep="best")
                # 洗版移动了文件, Navidrome 歌单要跟着更新
                if (_upg["stats"] or {}).get("removed_files"):
                    ctx.engine.mirror_player()
            except Exception as exc:  # noqa: BLE001
                _upg["error"] = str(exc)[:300]
            finally:
                _upg["running"] = False

        threading.Thread(target=_run, name="upgrade-all", daemon=True).start()
        return JSONResponse({"ok": True, "started": True})

    @app.get("/api/search")
    def search_tracks(q: str, _: None = Depends(auth)) -> JSONResponse:
        """面板内搜歌(多平台), 结果可直接单曲下载."""
        q = (q or "").strip()
        if not q:
            return JSONResponse({"results": [], "errors": {}})
        results: list[dict] = []
        errors: dict[str, str] = {}
        for name, p in ctx.provider_list().items():
            try:
                for t in p.search(q, limit=6):
                    results.append({
                        "provider": t.provider, "remote_id": str(t.remote_id),
                        "title": t.title, "artists": t.artist_str,
                        "album": t.album, "duration": t.duration,
                        "cover": t.cover_url,
                    })
            except Exception as exc:  # noqa: BLE001 - 单平台挂了不影响另一平台
                errors[p.display] = str(exc)[:120]
        return JSONResponse({"results": results[:12], "errors": errors})

    @app.post("/api/search/download")
    def search_download(payload: dict, _: None = Depends(auth)) -> JSONResponse:
        """把搜到的一首歌直接下载进库(后台执行, 结果走通知/下载记录)."""
        from ..providers.base import RemoteTrack

        required = ("provider", "remote_id", "title")
        if any(not payload.get(k) for k in required):
            return JSONResponse({"ok": False, "error": "参数不完整"}, status_code=400)
        track = RemoteTrack(
            provider=payload["provider"], remote_id=payload["remote_id"],
            title=payload["title"],
            artists=[a.strip() for a in str(payload.get("artists") or "").split(";") if a.strip()],
            album=payload.get("album") or "", duration=int(payload.get("duration") or 0),
            cover_url=payload.get("cover") or "",
        )

        def work() -> None:
            try:
                res = ctx.engine.download_single(track)
            except Exception as exc:  # noqa: BLE001
                ctx.db.log("error", "search", f"单曲下载异常: {track.title} - {exc}")
                return
            if res.get("ok"):
                ctx.db.log("info", "search",
                           f"单曲下载完成: {track.artist_str} - {track.title}"
                           + ("（库里已有，跳过）" if res.get("skipped") else ""))
            else:
                ctx.db.log("warn", "search",
                           f"单曲下载失败: {track.title} - {res.get('error', '')[:200]}")

        threading.Thread(target=work, name="mh-single-dl", daemon=True).start()
        return JSONResponse({"ok": True, "started": True, "title": track.title})

    @app.post("/api/tracks/{track_id}/delete")
    def delete_track(track_id: int, _: None = Depends(auth)) -> JSONResponse:
        """面板里手动删歌: 删文件 + 取消平台收藏. 不可撤销, 前端必须二次确认."""
        result = ctx.engine.delete_track(track_id)
        return JSONResponse(result, status_code=200 if result.get("ok") else 400)

    @app.get("/api/trash")
    def trash(_: None = Depends(auth)) -> JSONResponse:
        return JSONResponse(ctx.engine.trash_stats())

    @app.post("/api/trash/empty")
    def trash_empty(_: None = Depends(auth)) -> JSONResponse:
        return JSONResponse({"ok": True, **ctx.engine.trash_empty()})

    @app.get("/api/duplicates/upgrade-status")
    def upgrade_status(_: None = Depends(auth)) -> JSONResponse:
        return JSONResponse(_upg)

    @app.get("/api/selftest")
    def selftest(_: None = Depends(auth)) -> JSONResponse:
        out = {"providers": {}, "navidrome": {}}
        for name, p in ctx.provider_list().items():
            entry = {"status": p.status(), "search": None, "source": None}
            try:
                hits = p.search("test", limit=1)
                entry["search"] = bool(hits)
                if hits:
                    try:
                        src = p.source(hits[0], (p.cfg.quality_chain or ["standard"])[0])
                        entry["source"] = {"ok": True, "ext": src.ext, "quality": src.quality}
                    except Exception as exc:  # noqa: BLE001
                        entry["source"] = {"ok": False, "error": str(exc)[:200]}
            except Exception as exc:  # noqa: BLE001
                entry["search"] = False
                entry["error"] = str(exc)[:200]
            out["providers"][name] = entry
        if ctx.navidrome.available():
            out["navidrome"] = ctx.navidrome.probe()
        return JSONResponse(out)

    # ---------- 设置 ----------
    @app.get("/api/settings")
    def get_settings(_: None = Depends(auth)) -> JSONResponse:
        data = settings_mod.schema(ctx.cfg, ctx.db)
        data["readonly"] = {
            "music_dir": str(ctx.cfg.music_dir),
            "data_dir": str(ctx.cfg.data_dir),
            "note": "音乐目录/数据目录只能在 .env 里改, 面板不开放 —— 改错会伤到库",
        }
        data["password_set"] = bool(ctx.cfg.web_password)
        data["vip"] = {name: p.vip_status() for name, p in ctx.provider_list().items()}
        return JSONResponse(data)

    @app.post("/api/settings")
    async def save_settings(request: Request) -> JSONResponse:
        body = await request.json()
        values = body.get("values") or {}
        if not isinstance(values, dict):
            return JSONResponse({"ok": False, "error": "参数格式错误"}, status_code=400)
        changed, errors = settings_mod.save(ctx.cfg, ctx.db, {str(k): str(v) for k, v in values.items()})
        warnings = settings_mod.apply_runtime(ctx, changed)
        if changed:
            ctx.db.log("info", "settings", f"面板更新配置: {', '.join(changed)}")
        resp = JSONResponse({
            "ok": not errors, "changed": changed, "errors": errors, "warnings": warnings,
        })
        # 改密码时给当前会话续一个 Cookie, 免得立刻被踢出去
        if "web.password" in changed and ctx.cfg.web_password:
            resp.set_cookie(COOKIE, _key_of(ctx.cfg.web_password), httponly=True,
                            samesite="lax", max_age=30 * 86400)
        return resp

    @app.post("/api/settings/detect/qq-favorite")
    def detect_qq_favorite(_: None = Depends(auth)) -> JSONResponse:
        """自动查找 QQ『我喜欢』歌单 id (接口不直接暴露, 只能从歌单列表里找)."""
        p = ctx.providers.get("qq")
        if p is None:
            return JSONResponse({"ok": False, "error": "QQ 音乐未启用"}, status_code=400)
        try:
            pls = p.my_playlists()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)[:200]})
        cands = [{"id": x.remote_id, "name": x.name}
                 for x in pls if x.is_favorite or "喜欢" in (x.name or "")]
        return JSONResponse({"ok": bool(cands), "candidates": cands,
                             "total": len(pls)})

    @app.post("/api/settings/test")
    async def test_settings(request: Request) -> JSONResponse:
        body = await request.json()
        group = str(body.get("group") or "")
        if group == "platform":
            out: dict = {}
            for name, p in ctx.provider_list().items():
                entry: dict = {"status": p.status(), "vip": p.vip_status(refresh=True)}
                try:
                    hits = p.search("test", limit=1)
                    entry["search"] = bool(hits)
                except Exception as exc:  # noqa: BLE001
                    entry["search"] = False
                    entry["error"] = str(exc)[:160]
                out[p.display] = entry
            ok = all(e.get("search") for e in out.values())
            return JSONResponse({"ok": ok, "detail": out})
        if group == "navidrome":
            if not ctx.navidrome.available():
                return JSONResponse({"ok": False, "detail": {"error": "地址/用户名/密码未填全"}})
            probe = ctx.navidrome.probe()
            ok = bool(probe.get("native") or probe.get("subsonic"))
            return JSONResponse({"ok": ok, "detail": probe})
        if group == "telegram":
            if not ctx.cfg.telegram.token:
                return JSONResponse({"ok": False, "detail": {"error": "Token 未填"}})
            from ..tg.api import TgApi, TgError
            try:
                me = TgApi(ctx.cfg.telegram.token, ctx.cfg.telegram.proxy).me()
                return JSONResponse({"ok": True, "detail": {"username": me.get("username"),
                                                            "name": me.get("first_name")}})
            except TgError as exc:
                return JSONResponse({"ok": False, "detail": {"error": str(exc)[:200]}})
        if group == "notify":
            ok, msg = ctx.notifier.test()
            return JSONResponse({"ok": ok, "detail": {"message": msg}})
        return JSONResponse({"ok": False, "detail": {"error": "未知分组"}}, status_code=400)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse({"ok": True})

    return app
