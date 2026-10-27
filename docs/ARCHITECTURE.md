# 架构与同步算法

## 模块图

```
app/
├── config.py            配置(env + .env), 缺配置降级不崩溃
├── db.py                SQLite(WAL): tracks / playlists / playlist_items / actions / events
├── providers/
│   ├── base.py          平台契约: playlist / source / add_tracks / remove_tracks
│   ├── netease.py       网易云 weapi(两层AES+RSA), 官方 Web 接口, 无第三方依赖
│   ├── qq.py            QQ 音乐 Qzone 系接口 + musicu.fcg vkey
│   ├── mock.py          离线测试桩: 真 HTTP 服务吐音频
│   └── registry.py      装配 + 链接识别
├── downloader.py        音质回退 / 流式下载 / mutagen 校验 / 重试
├── organizer.py         命名规范 / 标签 / 封面 / 歌词 / 冲突处理 / 安全删除
├── sync.py              同步引擎(核心)
├── navidrome.py         Native API 优先 + Subsonic 兜底
├── runtime.py           装配容器 + 后台调度(独立线程, 不用调度框架)
├── tg/                  原生 Bot API 封装 + 精简机器人
├── web/                 FastAPI 面板(单文件 HTML, 无构建步骤)
└── main.py              CLI: serve / sync / selftest / doctor
```

## 关键抽象: "收藏" = "『我喜欢』歌单的成员关系"

两个平台的"喜欢"机制完全不同, 但都可以归结为歌单成员关系:

- 网易云: "我喜欢的音乐"是一个特殊歌单(`specialType=5`), 加/删用
  `/weapi/playlist/manipulate/tracks`(op=add/del), 这是官方 App 也在用的接口
- QQ 音乐: "我喜欢"是一个特殊 diss, 加/删用 songdir 系列接口(尽力而为, 失败进人工队列)

于是平台侧只需要 4 个原语: `playlist / add_tracks / remove_tracks / source`。
收藏双向同步 = 对"我喜欢"这个歌单做双向同步, **没有特判分支**。

## 同步周期(每 60 分钟, 手动可触发)

```
1. reconcile_local      扫描 MUSIC_DIR
   ├─ 文件没了 → 所有同路径曲目标记 missing → 入队 unfavorite 动作(回推平台)
   └─ 文件回来了 → missing 恢复 active
2. pull_playlists       逐个拉订阅歌单
   ├─ 新曲目 → 入库 status=pending
   ├─ 远端移除且无其他歌单引用 → 按策略删除本地文件
   └─ 更新 playlist_items(作为下一轮 diff 的基准)
3. download_pending     并发下载(attempts<3)
   staging 下载 → mutagen 校验 → 写标签 → 嵌封面 → 移入库 → 记录 active
4. push_actions         执行回推动作队列(失败退避重试, 5 次后进人工)
5. mirror_player        Navidrome: 全库 path→songId 索引, 按歌单镜像内容
```

## 数据一致性: 以文件系统为事实源

平台歌单、本地文件、播放器三方中, **本地文件系统是唯一事实源**:

- 平台 → 本地: 通过每轮 diff
- 本地 → 平台: 通过文件扫描发现"缺失"
- 本地 → 播放器: 通过 Navidrome 全库 path 索引做镜像

这保证了任何一边的误操作都能被下一轮周期纠正, 而不需要消息队列/Webhook 这种
"丢了就永远丢了"的机制。

## 删除的安全边界(为什么不会误删你的库)

删除一个文件必须同时满足:

1. `delete_policy != never`
2. `tracks.managed = 1`(即 MuseHub 自己下载的, 你原有的文件永远 managed=0)
3. 没有任何已订阅歌单引用它
4. 同路径下没有其他 active 记录仍被引用(两个平台下载的同一首歌)
5. 不在 dry_run

任何一条不满足 → 只记录日志, 不动手。

## 平台接口说明

### 网易云 (weapi)

- 请求体: `{"params": <双层AES-CBC base64>, "encSecKey": <RSA(随机密钥逆序), 256位hex>}`
- 曲目 URL: `/weapi/song/enhance/player/url/v1`, `level` 决定音质
- 未登录/VIP 无权限时返回空 url → 触发音质回退链

### QQ 音乐 (无需 sign 的接口族)

- 歌单: `fcg_ucc_getcdinfo_byids_cp.fcg`(公开歌单免登录)
- 音源: `musicu.fcg` 的 `vkey.GetVkeyServer`, `filename` 前缀决定音质
  (M500=128k / M800=320k / F000=flac / RS01=hires)
- 写操作走旧版 songdir 接口 —— 这是全项目唯一"尽力而为"的部分

## 机器人为什么不会卡死(对比原版的根因分析)

原版把同步/下载逻辑直接写在消息处理器里, TG API 一旦超时, 整个轮询循环被拖住。
MuseHub 的做法:

1. 处理器只做"读状态 + 派线程", 毫秒级返回
2. 轮询循环每轮独立 try/except, 单条消息异常不影响下一轮
3. 所有 HTTP 调用显式超时, 网络异常指数退避(5s→120s)
4. `update_id` offset 落库, 重启不重复消费
5. 同步引擎自带互斥, 重复触发直接跳过而不是排队堆积
