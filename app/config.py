"""配置加载.

优先级: 环境变量 > .env 文件 > 默认值.

设计原则:
  * 所有路径在启动时就 resolve 成绝对路径, 避免工作目录变化导致文件乱飞;
  * 危险开关 (删除策略 / dry-run) 默认取最保守值;
  * 缺配置不抛异常, 而是把对应能力标记为 "未启用", 让程序能带病启动并
    在 Web/TG 里明确告诉用户缺什么.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """极简 .env 解析, 不覆盖已存在的环境变量."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    v = _env(name).lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on", "y")


@dataclass
class NeteaseConfig:
    cookie: str = ""
    accounts: list = field(default_factory=list)   # [(昵称, cookie)] 多账号, 面板维护
    # 下载/取音源用哪个账号(留空=第一个). 订阅只负责"读歌单", 与这个解耦
    download_account: str = ""
    # 最高音质(面板里选), 实际下载按 quality_chain 逐级降级
    max_quality: str = "lossless"
    quality_chain: tuple[str, ...] = ("lossless", "exhigh", "higher", "standard")
    enabled: bool = True


@dataclass
class QQConfig:
    cookie: str = ""
    accounts: list = field(default_factory=list)
    download_account: str = ""   # 下载用账号(留空=第一个), 见 NeteaseConfig 说明
    uin: str = ""
    favorite_dissid: str = ""   # 『我喜欢』歌单 dissid, 用于收藏双向同步
    max_quality: str = "f000"
    quality_chain: tuple[str, ...] = ("f000", "m800", "m500")
    enabled: bool = True


@dataclass
class NavidromeConfig:
    url: str = ""
    username: str = ""
    password: str = ""
    enabled: bool = False
    playlist_prefix: str = ""  # 可选: 给镜像歌单加前缀, 如 "[自动] "


@dataclass
class TelegramConfig:
    token: str = ""
    admin_ids: tuple[int, ...] = ()
    enabled: bool = False
    proxy: str = ""
    poll_timeout: int = 25


@dataclass
class SyncConfig:
    interval_minutes: int = 60
    # never: 永不删除本地文件
    # managed_only: 只删本程序下载的文件 (默认, 最安全)
    # mirror: 严格跟随平台歌单 (仍不会删除非本程序下载的文件)
    delete_policy: str = "managed_only"
    dry_run: bool = True           # 首次运行默认只报告不执行
    download_concurrency: int = 2
    max_downloads_per_cycle: int = 50
    push_unfavorite: bool = True   # 本地文件被删 -> 取消平台收藏
    dedup: bool = True             # 下载前同曲去重(复用已有文件)
    notify: bool = True            # 同步结束后 Telegram 汇报
    reconcile_interval_minutes: int = 10  # 本地扫描(检测删除)的独立周期
    adopt_external: bool = True    # 自动纳管库里新增的文件(用户自己放的/别的工具下的)
    trash_keep: bool = True        # 洗版时差文件移入回收目录(可找回); False=直接删
    fallback_platform: bool = True # 本平台无音源时, 自动去其他平台搜同曲下载
    retry_failed_hours: int = 24   # 失败曲目多久后重试一次(0=永不重试)
    favorite_on_download: bool = False  # 面板搜歌下载后, 是否回写平台「我喜欢」


@dataclass
class NotifyConfig:
    """通知策略.

    为什么按事件类型分开开关:
      日常最需要的是"新歌入库"和"歌被删了"; 洗版/去重属于运维细节, 有人嫌吵;
      下载失败往往一次几十条, 有人只想看汇总。分开控制才不至于让人直接关掉通知。
    """
    enabled: bool = True
    telegram: bool = True          # 推送到 Telegram(复用已有机器人)
    webhook_url: str = ""          # 通用 Webhook: POST JSON, 可接 Emby/n8n/自建服务
    # 国内常用 IM 机器人(都是 webhook, 无需公网回连)
    wecom_url: str = ""            # 企业微信群机器人
    dingtalk_url: str = ""         # 钉钉群机器人(支持加签)
    dingtalk_secret: str = ""      # 钉钉「加签」密钥, 安全设置选了加签才需要
    feishu_url: str = ""           # 飞书自定义机器人
    feishu_secret: str = ""        # 飞书「签名校验」密钥
    verbose_tracks: bool = True    # 逐首列出新入库歌曲(关掉只报数量)
    playlist_changes: bool = True  # 歌单有增删
    track_added: bool = True       # 新歌入库
    track_removed: bool = True     # 歌曲被删(平台取消收藏 / 本地删除)
    download_failed: bool = True   # 下载失败
    dedup: bool = True             # 洗版/去重
    errors: bool = True            # 平台/播放器异常
    summary: bool = True           # 每轮同步汇总
    quiet_hours: str = ""          # 免打扰, 例如 "23:30-08:00", 留空=全天可发
    max_lines: int = 15            # 单条消息最多列多少首(超出折叠)


@dataclass
class OrganizerConfig:
    # 有专辑时的路径模板; 用 / 分隔层级, 占位符见 organizer.py
    album_template: str = "{albumartist}/{album}/{albumartist} - {title}"
    # 单曲(专辑名缺失/等于歌名)时的模板
    single_template: str = "{albumartist}/单曲/{albumartist} - {title}"
    single_album_name: str = "单曲"
    write_cover_file: bool = True
    write_lyrics_file: bool = True
    embed_cover: bool = True
    min_component_bytes: int = 120


@dataclass
class Config:
    data_dir: Path
    music_dir: Path
    staging_dir: Path
    log_dir: Path

    netease: NeteaseConfig = field(default_factory=NeteaseConfig)
    qq: QQConfig = field(default_factory=QQConfig)
    navidrome: NavidromeConfig = field(default_factory=NavidromeConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    organizer: OrganizerConfig = field(default_factory=OrganizerConfig)

    web_host: str = "0.0.0.0"
    web_port: int = 8080
    web_username: str = "admin"
    web_password: str = ""
    timezone: str = "Asia/Shanghai"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "musehub.db"

    @classmethod
    def load(cls, env_file: Path | None = None) -> "Config":
        if env_file is None:
            env_file = Path(os.environ.get("MUSEHUB_ENV_FILE", ".env"))
        _load_dotenv(env_file)

        data_dir = Path(_env("DATA_DIR", "./data")).expanduser().resolve()
        music_dir = Path(_env("MUSIC_DIR", "./music")).expanduser().resolve()
        staging_dir = Path(_env("STAGING_DIR", str(data_dir / "staging"))).expanduser().resolve()
        log_dir = Path(_env("LOG_DIR", str(data_dir / "logs"))).expanduser().resolve()
        for d in (data_dir, music_dir, staging_dir, log_dir):
            d.mkdir(parents=True, exist_ok=True)

        admin_ids = tuple(
            int(x) for x in _env("ADMIN_USER_ID").replace(" ", "").split(",") if x.strip().isdigit()
        )

        ne_chain = tuple(
            q.strip() for q in _env("NETEASE_QUALITY", "lossless,exhigh,higher,standard").split(",") if q.strip()
        )
        ne = NeteaseConfig(
            cookie=_env("NETEASE_COOKIE"),
            max_quality=_env("NETEASE_MAX_QUALITY", ne_chain[0] if ne_chain else "lossless"),
            quality_chain=ne_chain,
            enabled=_env_bool("NETEASE_ENABLED", True),
        )
        qq_chain = tuple(
            q.strip() for q in _env("QQ_QUALITY", "f000,m800,m500").split(",") if q.strip()
        )
        qq = QQConfig(
            cookie=_env("QQ_COOKIE"),
            uin=_env("QQ_UIN"),
            favorite_dissid=_env("QQ_FAVORITE_DISSID"),
            max_quality=_env("QQ_MAX_QUALITY", qq_chain[0] if qq_chain else "f000"),
            quality_chain=qq_chain,
            enabled=_env_bool("QQ_ENABLED", True),
        )
        nav = NavidromeConfig(
            url=_env("NAVIDROME_URL").rstrip("/"),
            username=_env("NAVIDROME_USERNAME"),
            password=_env("NAVIDROME_PASSWORD"),
            playlist_prefix=_env("NAVIDROME_PLAYLIST_PREFIX"),
            enabled=_env_bool("NAVIDROME_ENABLED", bool(_env("NAVIDROME_URL"))),
        )
        tg = TelegramConfig(
            token=_env("TELEGRAM_BOT_TOKEN"),
            admin_ids=admin_ids,
            proxy=_env("TELEGRAM_PROXY"),
            poll_timeout=_env_int("TELEGRAM_POLL_TIMEOUT", 25),
            enabled=_env_bool("TELEGRAM_ENABLED", bool(_env("TELEGRAM_BOT_TOKEN"))),
        )
        sync = SyncConfig(
            interval_minutes=_env_int("SYNC_INTERVAL_MINUTES", 60),
            delete_policy=_env("DELETE_POLICY", "managed_only"),
            dry_run=_env_bool("DRY_RUN", True),
            download_concurrency=max(1, _env_int("DOWNLOAD_CONCURRENCY", 2)),
            max_downloads_per_cycle=max(1, _env_int("MAX_DOWNLOADS_PER_CYCLE", 300)),
            push_unfavorite=_env_bool("PUSH_UNFAVORITE", True),
            dedup=_env_bool("DEDUP", True),
            notify=_env_bool("NOTIFY", True),
            adopt_external=_env_bool("ADOPT_EXTERNAL", True),
            trash_keep=_env_bool("TRASH_KEEP", True),
            fallback_platform=_env_bool("FALLBACK_PLATFORM", True),
            retry_failed_hours=_env_int("RETRY_FAILED_HOURS", 24),
            favorite_on_download=_env_bool("FAVORITE_ON_DOWNLOAD", False),
            reconcile_interval_minutes=max(1, _env_int("RECONCILE_INTERVAL_MINUTES", 10)),
        )
        org = OrganizerConfig(
            album_template=_env("PATH_TEMPLATE_ALBUM", "{albumartist}/{album}/{albumartist} - {title}"),
            single_template=_env("PATH_TEMPLATE_SINGLE", "{albumartist}/单曲/{albumartist} - {title}"),
            write_cover_file=_env_bool("WRITE_COVER_FILE", True),
            write_lyrics_file=_env_bool("WRITE_LYRICS_FILE", True),
            embed_cover=_env_bool("EMBED_COVER", True),
            min_component_bytes=_env_int("MAX_COMPONENT_BYTES", 120),
        )
        notify = NotifyConfig(
            enabled=_env_bool("NOTIFY", True),
            telegram=_env_bool("NOTIFY_TELEGRAM", True),
            webhook_url=_env("NOTIFY_WEBHOOK_URL", ""),
            quiet_hours=_env("NOTIFY_QUIET_HOURS", ""),
        )

        return cls(
            data_dir=data_dir,
            music_dir=music_dir,
            staging_dir=staging_dir,
            log_dir=log_dir,
            netease=ne,
            qq=qq,
            navidrome=nav,
            telegram=tg,
            sync=sync,
            notify=notify,
            organizer=org,
            web_host=_env("WEB_HOST", "0.0.0.0"),
            web_port=_env_int("WEB_PORT", 8080),
            web_username=_env("WEB_USERNAME", "admin"),
            web_password=_env("WEB_PASSWORD"),
            timezone=_env("TZ", "Asia/Shanghai"),
        )

    def capability_report(self) -> dict[str, str]:
        """启动自检: 告诉用户哪些能力可用/缺什么."""
        rep: dict[str, str] = {}
        rep["netease"] = "ok" if self.netease.cookie else "缺少 Cookie, 到 设置→音乐平台 粘贴"
        rep["qq"] = "ok" if self.qq.cookie else "缺少 Cookie, 到 设置→音乐平台 粘贴"
        rep["navidrome"] = (
            "ok" if (self.navidrome.url and self.navidrome.username and self.navidrome.password)
            else "未配置, 到 设置→播放器 填写"
        )
        rep["telegram"] = "ok" if self.telegram.token else "未配置, 到 设置→Telegram 填写"
        rep["mode"] = "DRY-RUN 只观察不执行" if self.sync.dry_run else f"实盘 / 删除策略={self.sync.delete_policy}"
        return rep
