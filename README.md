# MuseHub

> 自建的音乐中枢：订阅平台歌单 → 自动下载归档 → 镜像到 Navidrome，四方（平台 / 文件 / 播放器 / 收藏）保持一致。

一个单进程、零外部依赖（除 SQLite）的 Python 服务，替代 TGmusicBot 这类"下载机器人"，
把**歌单订阅、音质回退、标签整理、播放器镜像、收藏回推**做成一件事。

## 它解决什么问题

| 常见痛点 | MuseHub 的做法 |
|---|---|
| 下载器只管下，标签/封面/歌词一团乱 | 下载即归档：命名模板、标签写入、封面嵌入、`.lrc` 落地，全部在下完时完成 |
| 平台收藏和本地库各说各话 | 收藏双向同步：本地删了 → 回平台取消收藏；平台加了 → 本地跟着下 |
| 播放器里看不到平台歌单 | 把订阅歌单镜像成 Navidrome 歌单，增删自动跟随，还能按账号分发 |
| 机器人被网络超时拖死 | 处理器只派任务不干活，轮询独立 try/except + 指数退避 |

## 特性

- **订阅**：粘贴网易云 / QQ 音乐的歌单、专辑、榜单链接即可；支持多账号（一个平台多个 Cookie）
- **同步**：每轮 diff 平台与本地，新曲自动下载，远端移除按策略清理
- **下载**：音质回退链（无损 → 极高 → 较高 → 标准），流式下载 + mutagen 校验 + 失败重试
- **归档**：`艺人/专辑/曲目` 结构，模板可配；附封面、歌词文件；单曲自动归入「单曲」
- **播放器镜像**：Navidrome 原生 API 优先、Subsonic 兜底；歌单可改名、可指定镜像到哪个账号
- **收藏回推**：本地删文件 → 自动去平台取消收藏，失败进人工队列
- **库体检**：缺封面 / 缺歌词 / 重复文件 / 元数据缺失，一键补齐（只补空缺，绝不覆盖已有值）
- **元数据刮削**：按库里的平台 ID 精确补年份 / 音轨号 / 专辑 / 封面，默认 4 路并发
- **面板**：FastAPI + 单文件 HTML，无构建步骤；含实时进度、日志检索、配置热生效

## 界面

暖纸底 + 墨黑 + 单一朱红的「唱片封套」质感：衬线大标题、发丝分隔线、等宽数字与路径。

| 仪表盘 | 库体检 |
|---|---|
| ![仪表盘](docs/screenshots/dashboard.png) | ![库体检](docs/screenshots/library.png) |

| 歌单同步 | 设置 |
|---|---|
| ![歌单同步](docs/screenshots/playlists.png) | ![设置](docs/screenshots/settings.png) |

## 快速开始

Docker（推荐，NAS / 服务器）：

```bash
git clone <this-repo> musehub && cd musehub
cp .env.example .env && vi .env          # 填 Cookie 与 Navidrome 地址
docker compose up -d                     # 面板 http://<host>:8890
```

`docker-compose.yml` 里把挂载源换成你的真实音乐目录（容器内固定 `/music`），
Navidrome 也要挂同一个目录 —— 这是镜像能对上文件的前提。

本地直跑：

```bash
python -m venv .venv && .venv\Scripts\pip install -r requirements.txt   # Windows
cp .env.example .env
.venv\Scripts\python -m app serve
```

上线检查四步（CLI 与面板等价）：

```bash
python -m app doctor      # 配置体检
python -m app selftest    # 平台连通性：搜索 / 音源 / Navidrome
python -m app sync --dry  # 试运行，看它"将要"做什么
python -m app sync        # 确认无误再真跑
```

完整的凭据获取步骤见 [`docs/部署指南.md`](docs/部署指南.md)。

## 几个关键设计

**以文件系统为事实源。** 平台歌单、本地文件、播放器三方之中，本地文件是唯一真相：
平台 → 本地靠每轮 diff，本地 → 平台靠扫描发现"缺失"，本地 → 播放器靠 path 索引镜像。
任何一边的误操作都会被下一轮纠正，不需要"丢了就永远丢了"的消息队列。

**空值不覆盖已有值。** 平台列表接口经常不给年份、音轨号、专辑名。所有同步 / 导入路径
都遵守"空值不覆盖"，否则一次同步就能把刮削或人工修好的元数据冲干净。

**删除有硬边界。** 满足全部条件才会动手：`delete_policy != never`、`managed=1`（MuseHub 自己下的）、
没有任何已订阅歌单引用、同路径没有其他 active 记录、不在试运行。任一不满足只记日志。

**收藏没有特判分支。** 网易云和 QQ 的"喜欢"机制完全不同，但都能归结为"『我喜欢』这个歌单的成员关系"，
于是平台侧只需 4 个原语：`playlist / add_tracks / remove_tracks / source`。

更多实现细节见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

## 配置

面板「系统设置」里改，保存即热生效（不用重启）；也支持 `.env`。

| 项 | 说明 |
|---|---|
| `NETEASE_ACCOUNTS` / `QQ_ACCOUNTS` | 每行 `昵称=Cookie`，支持多账号；只填一行等于单账号 |
| `NAVIDROME_URL` / `USERNAME` / `PASSWORD` | 播放器镜像目标；不填则只下载整理，不动播放器 |
| `MUSIC_DIR` | 音乐库根目录（容器内固定 `/music`） |
| `SYNC_INTERVAL_MINUTES` | 同步周期，默认 60 |
| `DELETE_POLICY` | `never` / `trash` / `delete`，默认 `trash` |
| `DRY_RUN` | `1` 时只记录不执行，首次上线建议先开 |
| `SCRAPE_WORKERS` | 元数据刮削并发数，默认 4（1~8，被限流就调小） |
| `TELEGRAM_TOKEN` / `ADMIN_IDS` | 可选，机器人 |

## 已知边界

- **流派补不了**：网易云 / QQ / 酷狗 / 酷我的接口都不返回 genre 字段，这是数据源限制
- **QQ 部分曲目无音源**：数字专辑需单独购买、版权下架，接口返回 `104003`，绿钻也拿不到
- **本地老文件没有平台 ID**：`provider=local` 的曲目无法自动匹配元数据，需人工
- **网易云部分专辑没有发行日期**：多见于日文盘，`publishTime` 为空
- **QQ 收藏回推是"尽力而为"**：旧版 songdir 接口对部分账号不可用，失败会进人工队列

## 开发

```bash
.venv/Scripts/python -m unittest discover -s tests -q   # 101 个用例
.venv/Scripts/python scripts/check_js.py                # 前端语法自检
.venv/Scripts/python scripts/audit.py --base http://<host>:8890 --cookie mh_key=xxx
                                                        # 前端巡检：JS 报错 / 失败请求 / 未绑定事件的按钮
.venv/Scripts/python scripts/shot.py --url ... --out ... # 带登录态截图
```

测试全部离线：`app/providers/mock.py` 起一个真实的本地 HTTP 服务吐音频，
所以下载、归档、镜像整条链路都能在 CI 里跑通，不碰真实平台。

## 文档

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — 模块图、同步周期、数据一致性
- [`docs/部署指南.md`](docs/部署指南.md) — 三种部署方式、上线检查单、常见问题
- [`docs/对比TGmusicBot.md`](docs/对比TGmusicBot.md) — 为什么要自己写一个

## License

未指定。个人项目，自用为主。
