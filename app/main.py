"""入口.

  python -m app serve      启动 Web + 调度 + 机器人 (默认)
  python -m app sync       立刻同步一次后退出 (适合 cron)
  python -m app selftest   检查各平台连通性
  python -m app doctor     检查配置与目录
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import Config
from .db import Database
from .runtime import Context


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .web.routes import create_app

    cfg = Config.load()
    ctx = Context(cfg)

    print("=" * 60)
    print("MuseHub 启动")
    for k, v in cfg.capability_report().items():
        print(f"  {k:10s} {v}")
    print(f"  音乐库     {cfg.music_dir}")
    print(f"  数据目录   {cfg.data_dir}")
    if ctx.loaded_settings:
        print(f"  面板配置   {len(ctx.loaded_settings)} 项已覆盖 .env")
    print("=" * 60)

    ok, msg = ctx.start_bot()
    print(f"  telegram   {msg}")
    ctx.start_scheduler()
    app = create_app(ctx)
    try:
        uvicorn.run(app, host=cfg.web_host, port=cfg.web_port,
                    log_level="info", access_log=False)
    finally:
        ctx.stop()
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    cfg = Config.load()
    ctx = Context(cfg)
    if args.live:
        ctx.db.kv_set("dry_run", "0")
    if args.dry:
        ctx.db.kv_set("dry_run", "1")
    stats = ctx.engine.run(trigger="cli")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0 if "error" not in stats else 1


def cmd_selftest(_args: argparse.Namespace) -> int:
    cfg = Config.load()
    ctx = Context(cfg)
    ok = True
    print("— 平台 —")
    for name, p in ctx.providers.items():
        print(f"{p.display:8s} {p.status()}")
        try:
            hits = p.search("周杰伦", limit=1)
            print(f"         搜索: {'ok' if hits else '无结果'}"
                  + (f" -> {hits[0].title}" if hits else ""))
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"         搜索: 失败 {exc}")
        if hits:
            quality = (p.cfg.quality_chain or ["standard"])[0]
            try:
                src = p.source(hits[0], quality)
                print(f"         音源({quality}): ok {src.ext}")
            except Exception as exc:  # noqa: BLE001
                print(f"         音源({quality}): {exc}")
    print("— 播放器 —")
    if ctx.navidrome.available():
        probe = ctx.navidrome.probe()
        print(json.dumps(probe, ensure_ascii=False, indent=2))
        if not probe.get("native") and not probe.get("subsonic"):
            ok = False
    else:
        print("未配置 Navidrome (可跳过)")
    print("— 目录 —")
    print(f"music: {cfg.music_dir} exists={cfg.music_dir.exists()}")
    print(f"data : {cfg.data_dir} exists={cfg.data_dir.exists()}")
    return 0 if ok else 1


def cmd_import(args: argparse.Namespace) -> int:
    from .library_import import run as import_run

    cfg = Config.load()
    db = Database(cfg.db_path)
    stats = import_run(cfg.music_dir, db, apply=args.apply, limit=args.limit)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if not args.apply:
        print("（预览模式。加 --apply 真正写入数据库）")
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    cfg = Config.load()
    print("配置检查")
    for k, v in cfg.capability_report().items():
        print(f"  {k:10s} {v}")
    problems = []
    if not cfg.music_dir.exists():
        problems.append(f"音乐目录不存在: {cfg.music_dir}")
    elif not any(cfg.music_dir.iterdir()):
        problems.append(f"音乐目录是空的: {cfg.music_dir}")
    if cfg.sync.dry_run:
        print("  ⚠️  当前为试运行模式, 不会真正下载/删除. 确认无误后在 Web 面板切到实盘.")
    if problems:
        for p in problems:
            print(f"  ❌ {p}")
        return 1
    print("  ✅ 基础检查通过")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="musehub", description="本地音乐中枢")
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="启动服务(默认)")
    p_serve.set_defaults(func=cmd_serve)

    p_sync = sub.add_parser("sync", help="同步一次")
    p_sync.add_argument("--live", action="store_true", help="强制实盘(会真的下载/删除)")
    p_sync.add_argument("--dry", action="store_true", help="强制试运行")
    p_sync.set_defaults(func=cmd_sync)

    p_imp = sub.add_parser("import", help="把已有音乐库导入数据库(只读扫描)")
    p_imp.add_argument("--apply", action="store_true", help="真正写入")
    p_imp.add_argument("--limit", type=int, default=0, help="只处理前 N 个文件")
    p_imp.set_defaults(func=cmd_import)

    sub.add_parser("selftest", help="平台连通性自检").set_defaults(func=cmd_selftest)
    sub.add_parser("doctor", help="配置检查").set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["serve"])
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
