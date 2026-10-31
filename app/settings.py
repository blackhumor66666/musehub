"""面板可编辑的配置项.

设计取舍:
  * .env 仍然是"首次启动"的入口, 但所有常用项都能在 Web 面板里改;
  * 面板修改的值存进 SQLite(kv 表), 优先级高于 .env, 重启后依然生效;
  * 保存后立刻热应用到运行中的对象(provider 换 Cookie / Navidrome 重连 /
    机器人重启 / 调度器读新周期), 不需要重启容器;
  * 只有"路径类"配置(音乐目录)不给改 —— 那个改错会伤到库, 留在 .env 里。

为什么 dry_run / delete_policy 用裸 kv 键: 引擎早就在读它们, 保持兼容。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

GROUPS: list[tuple[str, str, str]] = [
    ("platform", "音乐平台", "网易云 / QQ 音乐账号凭据，决定能不能下载、能不能回推收藏"),
    ("navidrome", "播放器（Navidrome）",
     "配的是 Navidrome 音乐服务器（不是 Emby）。它只做一件事：把平台歌单镜像成 Navidrome 歌单，"
     "让你在播放器里直接看到「网易云歌单」「QQ歌单」，歌曲增删后自动跟着变。"
     "它不参与下载，也不统计曲库数量。不填 = 只下载整理文件，不动播放器。"),
    ("telegram", "Telegram", "机器人 Token 与管理员 ID，留空则不启动机器人"),
    ("notify", "通知", "哪些事件要推送、推到哪。默认走 Telegram，也可以加一个 Webhook 转到别的系统"),
    ("sync", "同步策略", "多久跑一次、删不删文件、要不要回推收藏"),
    ("organizer", "归档规则", "文件命名模板与附带文件"),
    ("web", "面板", "登录密码"),
]

# 分组配色/图标: 前端用色块区分不同板块
GROUP_META: dict[str, dict[str, str]] = {
    "platform": {"color": "#6366f1", "soft": "#eef0fe", "icon": "disc"},
    "navidrome": {"color": "#10b981", "soft": "#e7f8f1", "icon": "play"},
    "telegram": {"color": "#0ea5e9", "soft": "#e6f5fd", "icon": "send"},
    "notify": {"color": "#ec4899", "soft": "#fdeaf4", "icon": "bell"},
    "sync": {"color": "#f59e0b", "soft": "#fdf3e3", "icon": "refresh"},
    "organizer": {"color": "#8b5cf6", "soft": "#f1ecfe", "icon": "folder"},
    "web": {"color": "#f43f5e", "soft": "#feecef", "icon": "lock"},
}

# 音质档位: 与网易云/QQ 音乐 App 及 TGmusicBot 显示一致, 从高到低; 选中的档位以下自动逐级降级
NETEASE_LEVELS: list[tuple[str, str]] = [
    ("jymaster", "超清母带 Master · 需SVIP"),
    ("sky", "沉浸环绕声 · 需VIP"),
    ("jyeffect", "高清臻音/高清环绕声 · 需VIP"),
    ("hires", "高解析度无损 Hi-Res · 需VIP"),
    ("lossless", "无损 SQ (FLAC) · 需VIP"),
    ("exhigh", "极高 (320kbps MP3)"),
    ("higher", "较高 (192kbps MP3)"),
    ("standard", "标准 (128kbps MP3)"),
]
QQ_LEVELS: list[tuple[str, str]] = [
    ("sb00", "臻品母带 4.0 · 需超级会员（实验）"),
    ("q000", "臻品母带 2.0 · 需超级会员"),
    ("ai00", "臻品全景声 Dolby · 需超级会员"),
    ("rs01", "臻品母带 Hi-Res · 需超级会员"),
    ("f000", "无损 SQ (FLAC) · 需绿钻"),
    ("a000", "无损 APE · 需绿钻"),
    ("m800", "HQ 高品质 (320kbps MP3)"),
    ("c400", "标准音质 (128kbps M4A)"),
    ("m500", "标准 (128kbps MP3)"),
]
QUALITY_LEVELS: dict[str, list[tuple[str, str]]] = {"netease": NETEASE_LEVELS, "qq": QQ_LEVELS}


def chain_for(provider: str, max_quality: str) -> tuple[str, ...] | None:
    """选中档位 -> 从该档位往下的降级链."""
    levels = [c for c, _ in QUALITY_LEVELS.get(provider, [])]
    if max_quality not in levels:
        return None
    return tuple(levels[levels.index(max_quality):])


def quality_label(provider: str, code: str) -> str:
    for c, label in QUALITY_LEVELS.get(provider, []):
        if c == code:
            return label
    return code

# kind: text | secret | cookie | number | bool | select
FIELDS: list["Field"] = []


@dataclass
class Field:
    key: str
    group: str
    label: str
    kind: str = "text"
    help: str = ""
    options: list[tuple[str, str]] = field(default_factory=list)
    placeholder: str = ""
    min: float | None = None
    max: float | None = None
    secret: bool = False
    hidden: bool = False   # 已废弃/被合并的字段: 不在面板显示, 仅保留兼容读取

    def __post_init__(self) -> None:
        FIELDS.append(self)


# ---------------- 字段定义 ----------------
Field("netease.cookie", "platform", "网易云 Cookie", "cookie",
      "浏览器登录 music.163.com → F12 → Network → 任意请求 → 复制 Cookie 整行。"
      "浏览器登录 music.163.com → F12 → Network → 任意请求 → 复制整行 Cookie。"
      "必须含 MUSIC_U；**填带黑胶会员的账号**。不填只能搜索, 下不了高音质。",
      placeholder="MUSIC_U=...; __csrf=...")
Field("netease.accounts", "platform", "网易云账号（旧·多账号）", "accounts",
      "每行一个账号：昵称=Cookie。例如:\n我=MUSIC_U=xxx; __csrf=yyy\n老婆=MUSIC_U=zzz\n"
      "已合并到上面的单个 Cookie，这里仅为兼容保留。", hidden=True)
Field("qq.accounts", "platform", "QQ 音乐账号（旧·多账号）", "accounts",
      "已合并到上面的单个 Cookie，这里仅为兼容保留。", hidden=True)
Field("netease.max_quality", "platform", "网易云下载音质上限", "quality",
      "按网易云 App 里的叫法选择。选「无损音质」表示优先下无损；某首歌没有无损时自动降到极高 → 较高 → 标准。",
      options=NETEASE_LEVELS)

Field("qq.cookie", "platform", "QQ 音乐 Cookie", "cookie",
      "浏览器登录 y.qq.com 后复制整行 Cookie。**填带绿钻的账号**；公开歌单免登录可读。<br>"
      "⚠️ 用<b>微信扫码</b>登录的 Cookie 没有 skey，只能读歌单/下歌，<b>无法自动取消收藏</b>；"
      "换成 <b>QQ 号密码登录</b> 再复制一次 Cookie，就能自动取消收藏了。",
      placeholder="pgv_pvid=...; qm_keyst=...")
Field("qq.uin", "platform", "QQ 号", "text",
      "一般能从 Cookie 自动解析, 不用填。", hidden=True)
Field("qq.favorite_dissid", "platform", "『我喜欢』歌单 ID", "text",
      "只有 QQ 需要填：网易云的『我喜欢』能通过接口自动识别，QQ 的接口不返回，只能手填一次。"
      "打开 y.qq.com 的『我喜欢』歌单，地址栏 /playlist/<数字> 就是它。可点右侧「自动查找」。")
Field("qq.max_quality", "platform", "QQ 音乐下载音质上限", "quality",
      "按 QQ 音乐 App 里的叫法选择。选「无损音质（FLAC）」表示优先下无损；没有无损时自动降到高品质 320k → 标准 128k。",
      options=QQ_LEVELS)

Field("navidrome.url", "navidrome", "Navidrome 地址", "text",
      "例如 http://192.168.5.13:4533。注意这是 Navidrome，不是 Emby。留空 = 不同步播放器。")
Field("navidrome.username", "navidrome", "Navidrome 用户名", "text",
      "Navidrome 后台的登录账号（不是 Emby 账号）")
Field("navidrome.password", "navidrome", "Navidrome 密码", "secret", secret=True)
Field("navidrome.playlist_prefix", "navidrome", "歌单名前缀", "text",
      "镜像歌单的命名前缀, 例如填「[自动] 」, 留空则直接用原歌单名。")

Field("telegram.token", "telegram", "Bot Token", "secret",
      "找 @BotFather 创建机器人获取。留空则不启动机器人。", secret=True)
Field("telegram.admin_ids", "telegram", "管理员用户 ID", "text",
      "你的数字 ID, 多个用逗号分隔。找 @userinfobot 查。留空 = 不校验权限(不推荐)。")
Field("telegram.proxy", "telegram", "代理", "text",
      "国内访问 Telegram 需要, 例如 http://192.168.5.5:7890")

Field("notify.enabled", "notify", "通知总开关", "bool",
      "关掉后所有通知都停（包括同步汇总）。单项开关见下面。")
Field("notify.telegram", "notify", "推送到 Telegram", "bool",
      "复用上面配好的机器人。机器人没启动时这条不生效，但 Webhook 仍然会发。")
Field("notify.webhook_url", "notify", "Webhook 地址", "text",
      "可选。填了会把事件以 JSON POST 过去：{\"source\":\"musehub\",\"ts\":...,\"event\":\"added\",\"text\":...}。"
      "可接 n8n、自建服务、企业微信中转。"
      "注：不需要绕 Emby —— 收藏增删、洗版、回推这些事件 Emby 根本看不到，直连更准。",
      placeholder="http://192.168.5.13:5678/webhook/musehub")
Field("notify.wecom_url", "notify", "企业微信群机器人 Webhook", "text",
      "企业微信群 → 右上角 … → 群机器人 → 添加 → 复制 Webhook 地址。")
Field("notify.dingtalk_url", "notify", "钉钉群机器人 Webhook", "text",
      "钉钉群 → 群设置 → 机器人 → 添加自定义机器人。安全设置若选「加签」，把密钥也填上。")
Field("notify.dingtalk_secret", "notify", "钉钉加签密钥(可选)", "text",
      "安全设置选「加签」时必填，形如 SECxxxxx。")
Field("notify.feishu_url", "notify", "飞书群机器人 Webhook", "text",
      "飞书群 → 设置 → 群机器人 → 添加自定义机器人。")
Field("notify.feishu_secret", "notify", "飞书签名密钥(可选)", "text",
      "开启「签名校验」时必填。")
Field("notify.verbose_tracks", "notify", "逐首列出新入库歌曲", "bool",
      "开 = 消息里列出歌名；关 = 只报「新入库 N 首」。回填几百首时建议关掉。")
Field("notify.playlist_changes", "notify", "歌单增删", "bool",
      "平台歌单有歌加入或移除时通知，例如「我喜欢的音乐: +3 / -1」")
Field("notify.track_added", "notify", "新歌入库", "bool")
Field("notify.track_removed", "notify", "歌曲删除", "bool",
      "两种情况都会通知：平台取消收藏导致本地文件被删；你手动删了本地文件（会注明是否已排队取消平台收藏）")
Field("notify.download_failed", "notify", "下载失败", "bool",
      "包含失败原因（无音源/会员限制/归档失败等）")
Field("notify.dedup", "notify", "洗版 / 去重", "bool",
      "同一首歌出现多个版本时的自动升级、复用、回收目录清空")
Field("notify.errors", "notify", "异常告警", "bool",
      "接口报错、取消收藏回推失败（需要人工处理）等")
Field("notify.summary", "notify", "每轮同步汇总", "bool",
      "每轮结束发一条「入库/失败/删除/取消收藏/耗时」。什么都没发生的轮次不发。")
Field("notify.quiet_hours", "notify", "免打扰时段", "text",
      "例如 23:30-08:00。期间的消息会攒起来，等免打扰结束后的第一轮合并成一条补发。留空 = 全天可发。",
      placeholder="23:30-08:00")
Field("notify.max_lines", "notify", "单条消息最多列几首", "number", min=1, max=50,
      help="超出的部分折叠成「…还有 N 条」，避免一条消息几千字被 Telegram 截断")

Field("sync.interval_minutes", "sync", "全量同步周期(分钟)", "number", min=5, max=1440,
      help="多久拉一次平台歌单并下载新歌")
Field("sync.reconcile_interval_minutes", "sync", "本地扫描周期(分钟)", "number", min=2, max=1440,
      help="多久扫一次本地库, 用于发现「你手动删了文件」")
Field("sync.dry_run", "sync", "试运行模式", "bool",
      "开 = 只记录「将会做什么」, 不下载、不删除、不回推。", hidden=True)
Field("sync.delete_policy", "sync", "删除策略", "select",
      "never 永不删 / managed_only 只删本程序下载的(推荐) / mirror 严格镜像歌单",
      options=[("never", "never 永不删除"), ("managed_only", "managed_only 只删自己下载的"),
               ("mirror", "mirror 严格跟随（仍不动你原有的文件）")])
Field("sync.push_unfavorite", "sync", "本地删文件后取消平台收藏", "bool",
      "开启后, 你删掉本地文件, 下一轮会自动去平台取消收藏。"
      "注意: 只是把文件挪到别的目录/改名不算删除(会自动跟随新位置), "
      "同一首歌在库里还有别的副本时也不会取消收藏。")
Field("sync.dedup", "sync", "同曲去重", "bool",
      "下载前先查库里是否已有同一首歌(歌名+主艺人+时长匹配), 有就直接复用已有文件, 不重复下载")
Field("sync.adopt_external", "sync", "自动纳管库里新增的文件", "bool",
      "开启后, 你用别的工具(或手动)往音乐库里放歌, 下一轮会自动登记为「原有文件」——"
      "不会被删除, 而且下载新歌时能据此去重, 不会重复下一份")
Field("sync.fallback_platform", "sync", "跨平台回退下载", "bool",
      "开启后, 网易云没有某首歌的音源时, 自动去 QQ 音乐搜索同一首歌下载（严格匹配歌名+歌手+时长±5秒，防止拿到翻唱）；反之亦然")
Field("sync.trash_keep", "sync", "洗版保留回收副本", "bool",
      "开（默认）= 差音质文件移入 .musehub_trash，后悔能找回；"
      "关 = 洗版时直接删除，不占空间。回收目录可在「歌单同步 → 重复曲目」底部一键清空。")
Field("sync.retry_failed_hours", "sync", "失败曲目重试间隔(小时)", "number", min=0, max=720,
      help="下载失败的歌多久后再试一次(音源可能后来上架/会员状态变化)。0 = 永不重试。每轮最多补试 20 首。")
Field("sync.favorite_on_download", "sync", "搜歌下载后自动收藏", "bool",
      "面板「搜歌下载」把歌下进库之后, 是否顺手在对应平台点个红心(加入「我喜欢」)。"
      "关（默认）= 只入库不碰你的账号, 这首歌与平台收藏完全无关；"
      "开 = 形成「本地有 = 平台也收藏」的闭环, 以后在平台取消收藏也会连带删本地文件。")
Field("sync.download_concurrency", "sync", "并发下载数", "number", min=1, max=8)
Field("sync.max_downloads_per_cycle", "sync", "每轮最多下载", "number", min=1, max=500)

Field("organizer.album_template", "organizer", "专辑路径模板", "text",
      "占位符: {albumartist} {album} {title} {year} {track}")
Field("organizer.single_template", "organizer", "单曲路径模板", "text",
      "专辑名缺失或等于歌名时使用")
Field("organizer.write_cover_file", "organizer", "写 cover.jpg", "bool")
Field("organizer.write_lyrics_file", "organizer", "写 .lrc 歌词", "bool")
Field("organizer.embed_cover", "organizer", "内嵌封面到音频", "bool")

Field("web.password", "web", "面板密码", "secret",
      "改完立即生效, 当前会话会自动续期。留空 = 不校验(仅限本机使用)。", secret=True)

FIELDS_BY_KEY = {f.key: f for f in FIELDS}

# 这些键历史上就用裸 kv 名, 保持兼容
_LEGACY_KEYS = {"sync.dry_run": "dry_run", "sync.delete_policy": "delete_policy"}

# 配置对象里不是"分组.字段"结构的项(Config 上是扁平属性)
_ATTR_ALIASES = {"web.password": "web_password"}

# ---------------- 多账号 ----------------
# Cookie 里常见的键名。用来判断"这一行是裸 Cookie"还是"昵称=Cookie"。
# 踩过的坑: 用户直接粘贴整行 Cookie(pgv_pvid=...; qm_keyst=...) 时,
# 旧逻辑按第一个 "=" 拆成 name="pgv_pvid"、cookie="2423591868; ...",
# 结果账号名变成 pgv_pvid, Cookie 也被截断。
COOKIE_KEY_HINTS = {
    "pgv_pvid", "music_u", "music_a", "__csrf", "qm_keyst", "qqmusic_key",
    "uin", "wxuin", "p_uin", "luin", "_ntes_nnid", "nmtid", "fqm_pvqid",
    "fqm_sessionid", "_qimei_uuid42", "psrf_qqopenid", "psrf_qqaccess_token",
    "euin", "pgv_info", "qqmusic_key_", "login_type",
}


def _starts_with_cookie_key(line: str) -> bool:
    head = line.split("=", 1)[0].strip().lower()
    return head in COOKIE_KEY_HINTS


def parse_accounts(raw: str) -> list[tuple[str, str]]:
    """每行一个账号: 昵称=Cookie; 直接贴整行 Cookie 也能识别(自动命名)."""
    out: list[tuple[str, str]] = []
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if _starts_with_cookie_key(line):
            # 裸 Cookie: 整行都是凭据, 不能按第一个 "=" 拆
            name = "默认" if not out else f"账号{len(out) + 1}"
            cookie = line
        elif "=" in line:
            name, _, cookie = line.partition("=")
            name = name.strip() or ("默认" if not out else f"账号{len(out) + 1}")
            cookie = cookie.strip()
        else:
            name, cookie = ("默认" if not out else f"账号{len(out) + 1}"), line
        if cookie:
            out.append((name, cookie))
    return out


def render_accounts(accs) -> str:
    return "\n".join(f"{n}={c}" for n, c in (accs or []))


def kv_key(f: Field) -> str:
    return _LEGACY_KEYS.get(f.key, "cfg:" + f.key)


# ---------------- 读写 ----------------
def _to_attr(f: Field, raw: str) -> Any:
    if f.kind == "bool":
        return str(raw).lower() in ("1", "true", "yes", "on")
    if f.kind == "csv":
        return tuple(x.strip() for x in str(raw).replace("，", ",").split(",") if x.strip())
    if f.kind == "number":
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return 0
    if f.kind == "accounts":
        return parse_accounts(raw)
    # 文本/密钥/COOKIE 一律去首尾空白 —— 粘贴时带空格曾导致代理 URL 解析失败
    return str(raw).strip()


def _from_attr(f: Field, val: Any) -> str:
    if f.kind == "bool":
        return "1" if val else "0"
    if f.kind == "csv":
        return ",".join(val or ())
    if f.kind == "accounts":
        return render_accounts(val)
    if f.key == "telegram.admin_ids":
        return ",".join(str(x) for x in (val or ()))
    return "" if val is None else str(val)


def _get_section(cfg: Any, key: str) -> tuple[Any, str]:
    if key in _ATTR_ALIASES:
        return cfg, _ATTR_ALIASES[key]
    section, attr = key.split(".", 1)
    return getattr(cfg, section), attr


def load_into(cfg: Any, db: Any) -> list[str]:
    """启动时把面板里存过的值覆盖到 Config 上. 返回被覆盖的键."""
    # 账号只保留一份(单会员号)。历史遗留的多账号列表合并进 cookie 后清空,
    # 否则会出现"同一个号被当成两个账号", 面板上还多一堆看不懂的字段。
    for prov in ("netease", "qq"):
        acc_f = FIELDS_BY_KEY.get(f"{prov}.accounts")
        ck_f = FIELDS_BY_KEY.get(f"{prov}.cookie")
        if acc_f is None or ck_f is None:
            continue
        raw_acc = (db.kv_get(kv_key(acc_f), "") or "").strip()
        accs = parse_accounts(raw_acc)
        cookie = (db.kv_get(kv_key(ck_f), "") or "").strip()
        if accs and not cookie:
            first = accs[0][1]
            if first:
                db.kv_set(kv_key(ck_f), first)
                cookie = first
        if accs and cookie:
            db.kv_set(kv_key(acc_f), "")     # 已合并, 清空多账号
    applied: list[str] = []
    for f in FIELDS:
        row = db.query_one("SELECT value FROM kv WHERE key=?", (kv_key(f),))
        if row is None:
            continue
        try:
            obj, attr = _get_section(cfg, f.key)
            if f.key == "telegram.admin_ids":
                ids = tuple(int(x) for x in str(row["value"]).replace(" ", "").split(",") if x.isdigit())
                setattr(obj, attr, ids)
            else:
                value = _to_attr(f, row["value"])
                setattr(obj, attr, value)
                if f.kind == "quality":
                    chain = chain_for(f.key.split(".")[0], str(value))
                    if chain:
                        setattr(obj, "quality_chain", chain)
            applied.append(f.key)
        except (AttributeError, ValueError):
            continue
    return applied


def current_values(cfg: Any, db: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in FIELDS:
        row = db.query_one("SELECT value FROM kv WHERE key=?", (kv_key(f),))
        if row is not None:
            out[f.key] = str(row["value"])
        else:
            try:
                obj, attr = _get_section(cfg, f.key)
                val = getattr(obj, attr)
                if f.kind == "accounts" and not val:
                    # 首次展示: 把单账号 Cookie 显式转成一行, 让用户看清结构
                    cookie = getattr(obj, "cookie", "")
                    if cookie:
                        val = [("默认", cookie)]
                out[f.key] = _from_attr(f, val)
            except AttributeError:
                out[f.key] = ""
    return out


def schema(cfg: Any, db: Any) -> dict:
    values = current_values(cfg, db)
    groups = []
    for gid, label, desc in GROUPS:
        fields = []
        for f in FIELDS:
            if f.group != gid or f.hidden:
                continue
            fields.append({
                "key": f.key, "label": f.label, "kind": f.kind, "help": f.help,
                "options": [{"value": v, "label": lb} for v, lb in f.options],
                "placeholder": f.placeholder, "value": values.get(f.key, ""),
                "min": f.min, "max": f.max,
            })
        meta = GROUP_META.get(gid, {})
        groups.append({"id": gid, "label": label, "desc": desc, "fields": fields,
                       "color": meta.get("color", "#6366f1"),
                       "soft": meta.get("soft", "#eef0fe"),
                       "icon": meta.get("icon", "grid")})
    return {"groups": groups}


def validate(key: str, raw: str) -> str:
    """返回错误信息, 空字符串表示通过."""
    f = FIELDS_BY_KEY.get(key)
    if f is None:
        return f"未知配置项 {key}"
    val = str(raw).strip()
    if key == "navidrome.url" and val and not val.startswith(("http://", "https://")):
        return "地址必须以 http:// 或 https:// 开头"
    if f.kind == "number":
        try:
            num = int(float(val)) if val else 0
        except ValueError:
            return "必须是数字"
        if f.min is not None and num < f.min:
            return f"不能小于 {f.min:g}"
        if f.max is not None and num > f.max:
            return f"不能大于 {f.max:g}"
    if key == "telegram.admin_ids" and val:
        for part in val.replace(" ", "").split(","):
            if part and not part.isdigit():
                return "只能是数字 ID, 多个用逗号分隔"
    if f.kind == "quality":
        valid = [c for c, _ in QUALITY_LEVELS.get(key.split(".")[0], [])]
        if val not in valid:
            return "不是合法的音质档位"
    if key == "telegram.token" and val and not re.match(r"^\d{6,15}:[\w-]{20,}$", val):
        return "Token 格式不对: 应为 数字:密钥 (形如 123456789:AAFxxx)。"
        + " 你填的内容缺少冒号前的数字段, 请从 @BotFather 完整复制"
    if f.kind == "accounts" and val:
        for line in val.splitlines():
            line = line.strip()
            if line and "=" not in line and len(line) < 40:
                return f"账号行格式应为 昵称=Cookie：「{line[:20]}…」缺少 ="
    if key.endswith("_template") and val and "{title}" not in val:
        return "模板里必须包含 {title}"
    if key == "notify.quiet_hours" and val:
        from .notify import parse_quiet
        if parse_quiet(val) is None:
            return "格式应为 23:30-08:00（24 小时制，中间一个短横线）"
    if key == "notify.webhook_url" and val and not val.startswith(("http://", "https://")):
        return "Webhook 地址必须以 http:// 或 https:// 开头"
    return ""


def _sync_cookie_accounts(f: "Field", db: Any, new_val: Any) -> None:
    """让「单账号 Cookie」和「多账号列表」保持一致.

    历史上面板有两个入口(旧单 Cookie / 多账号), 而运行期只认多账号,
    用户把新 Cookie 粘到旧入口时会"保存成功但不生效"(线上踩过)。
    """
    provider = f.key.split(".", 1)[0]
    acc_f = FIELDS_BY_KEY.get(f"{provider}.accounts")
    ck_f = FIELDS_BY_KEY.get(f"{provider}.cookie")
    if f.kind == "cookie" and acc_f is not None:
        cookie = str(new_val or "").strip()
        if not cookie:
            return
        accs = parse_accounts(db.kv_get(kv_key(acc_f), ""))
        out: list[tuple[str, str]] = []
        done = False
        for name, ck in accs:
            if name == "默认":
                out.append(("默认", cookie)); done = True
            else:
                out.append((name, ck))
        if not done:
            out.insert(0, ("默认", cookie))
        db.kv_set(kv_key(acc_f), render_accounts(out))
    elif f.kind == "accounts" and ck_f is not None:
        # new_val 此时已经是解析好的列表(save 里设过的 attr), 不要再 parse 一次
        if isinstance(new_val, (list, tuple)):
            accs = [(str(n), str(c)) for n, c in new_val]
        else:
            accs = parse_accounts(str(new_val or ""))
        if not accs:
            return
        cookie = next((c for n, c in accs if n == "默认"), accs[0][1])
        db.kv_set(kv_key(ck_f), cookie)


def save(cfg: Any, db: Any, payload: dict[str, str]) -> tuple[list[str], list[str]]:
    """写入并热应用. 返回 (changed_keys, errors)."""
    changed: list[str] = []
    errors: list[str] = []
    for key, raw in payload.items():
        f = FIELDS_BY_KEY.get(key)
        if f is None:
            continue
        err = validate(key, raw)
        if err:
            errors.append(f"{f.label}: {err}")
            continue
        obj, attr = _get_section(cfg, key)
        if f.key == "telegram.admin_ids":
            new_val: Any = tuple(int(x) for x in str(raw).replace(" ", "").split(",") if x.isdigit())
        else:
            new_val = _to_attr(f, raw)
        if getattr(obj, attr, None) == new_val:
            continue
        setattr(obj, attr, new_val)
        if f.kind == "quality":
            chain = chain_for(key.split(".")[0], str(new_val))
            if chain:
                setattr(obj, "quality_chain", chain)
        db.kv_set(kv_key(f), _from_attr(f, new_val))
        _sync_cookie_accounts(f, db, new_val)
        changed.append(key)
    if changed:
        load_into(cfg, db)   # 回读(含上面同步的对应字段), 避免内存与库里不一致
    return changed, errors


def apply_runtime(ctx: Any, changed: list[str]) -> list[str]:
    """把改过的配置推给正在运行的对象."""
    warnings: list[str] = []
    if not changed:
        return warnings

    for p in ctx.providers.values():
        try:
            p.reload()
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{p.display} 重新加载失败: {exc}")

    # 改了账号相关配置: 会员状态与登录态缓存必须作废, 否则面板会继续显示旧结论
    # (线上踩过: 换了新 Cookie, 界面还挂着"Cookie 已过期"最长 1 小时)
    touched_platforms = {k.split(".", 1)[0] for k in changed
                         if k.split(".", 1)[0] in ("netease", "qq")}
    for plat in touched_platforms:
        ctx.db.kv_set(f"vip:{plat}", "")
    if touched_platforms:
        try:
            ctx.db.kv_set("cookie_health", "")
            import threading
            threading.Thread(target=ctx.check_cookies, kwargs={"alert": False},
                             daemon=True, name="cookie-recheck").start()
        except Exception as exc:  # noqa: BLE001 - 体检失败不该阻塞保存
            warnings.append(f"登录态重新检查失败: {exc}")

    if any(k.startswith("navidrome.") for k in changed):
        ctx.navidrome.reconfigure()
        if ctx.navidrome.available():
            probe = ctx.navidrome.probe()
            if not probe.get("native") and not probe.get("subsonic"):
                warnings.append("Navidrome 配置已保存, 但连接测试未通过: "
                                + str(probe.get("native_error") or probe.get("subsonic_error")))

    if any(k.startswith("telegram.") for k in changed):
        ok, msg = ctx.restart_bot()
        if not ok:
            warnings.append(f"Telegram 机器人未启动: {msg}")

    if any(k.startswith("netease.") for k in changed):
        p = ctx.providers.get("netease")
        if p and not p.logged_in():
            warnings.append("网易云 Cookie 为空, 只能搜索不能下载")

    if any(k.startswith("qq.") for k in changed):
        p = ctx.providers.get("qq")
        if p and not p.logged_in():
            warnings.append("QQ Cookie 为空, 只能读公开歌单")

    if "sync.interval_minutes" in changed or "sync.reconcile_interval_minutes" in changed:
        ctx.reschedule()
    return warnings
