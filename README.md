# LET Offers Monitor

监控 [LowEndTalk](https://lowendtalk.com) 的 **Offers** 版块，按自定义关键词筛选新帖，然后通过你部署在 Cloudflare 的 [notify-gateway](https://github.com/2Bdou/notify-gateway) 推送到邮件 / Telegram。

- 纯 Python 标准库，无第三方依赖，常驻内存约 30–50 MB
- 支持关键词、排除词、标题 / 正文匹配、正则
- 每轮聚合为一条通知，不会一个帖子发一条
- 用 GUID 去重，状态存本地 JSON，重启不重复推送
- 支持 ETag / Last-Modified（LET 目前不返回这两个头，实测每轮仍会拉约 200 KB）
- 自带 `GET /health`，方便 ecloudserv 判断容器状态

## 工作方式

```text
LowEndTalk Offers RSS
        │  每 10 分钟（可调），带 ETag 条件请求
        ▼
main.py ── 解析 ── 关键词过滤 ── GUID 去重
        │
        ▼
POST {NOTIFY_URL}  (Authorization: Bearer {NOTIFY_TOKEN})
        │
        ▼
notify-gateway ── 邮件 / Telegram
```

每轮最多推 `MAX_PER_RUN` 条命中，剩余的留在下一轮继续，不会丢。没有新帖或没有命中时不会发消息。

## 快速开始（本地）

```bash
cp .env.example .env
# 编辑 .env，至少填 NOTIFY_URL 和 NOTIFY_TOKEN
python3 main.py --test-notify     # 发一条测试通知
python3 main.py --once --dry-run  # 只抓取并打印命中，不发送、不改状态
python3 main.py --once            # 真正跑一轮
python3 main.py                   # 常驻，每 INTERVAL_MIN 分钟一轮
```

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `FEED_URL` | `https://lowendtalk.com/categories/offers/feed.rss` | 只监控 Offers；换成别的分类也可以 |
| `NOTIFY_URL` | — | 网关项目详情页的完整地址，以 `/api/notify` 结尾 |
| `NOTIFY_TOKEN` | — | 网关项目的 API Key，只放环境变量 |
| `KEYWORDS` | 空 | 逗号分隔，大小写不敏感；空 = 所有新帖 |
| `EXCLUDE_KEYWORDS` | 空 | 命中任一排除词就丢弃，优先级高于关键词 |
| `MATCH_FIELDS` | `title` | `title` 或 `title,body`；正文匹配更容易误报 |
| `SOURCE` | `let-offers` | 网关里显示的来源，最长 80 |
| `INTERVAL_MIN` | `10` | 轮询间隔，最小 1 分钟 |
| `MAX_PER_RUN` | `10` | 每轮最多推送几条，剩余下一轮继续 |
| `BOOTSTRAP` | `skip` | 首次运行：`skip` 把现有帖子标记为已读；`notify` 立即推送当前命中 |
| `STATE_FILE` | `state.json` | 去重状态文件，建议放在持久磁盘 |
| `GATEWAY_CHANNEL` | 空 | 可选 `email`、`telegram`；不填则由网关项目开关决定 |
| `PORT` | `3000` | 健康检查端口，ecloudserv 会注入 |
| `REQUEST_TIMEOUT` | `20` | 拉取 RSS 的超时秒数 |
| `DRY_RUN` | `0` | `1` = 只打印命中，不发送、不改状态 |

### 关键词写法

```dotenv
# 普通词，逗号分隔
KEYWORDS=cn2,9929,cmin2,japan,tokyo,osaka,storage,hdd,€

# 正则：/pattern/flags
KEYWORDS=/cn2|9929|cmin2/i,/(tokyo|osaka)/i,€

# 排除掉收机器、求推荐的帖子
EXCLUDE_KEYWORDS=wtb,looking for,request
```

注意逗号是分隔符，正则里不要写逗号；需要“或”时用 `|`。

### 两套预设（2026-10-25 实测）

**A. 中国优化线路 / 亚洲** —— 更精准，适合建站、回国线路

```dotenv
KEYWORDS=cn2,9929,cmin2,japan,tokyo,osaka,hong kong,singapore
EXCLUDE_KEYWORDS=wtb,looking for,request
```

当前 Offers 里命中约 5 条。CN2 / 9929 本身不常出现，属于“等鱼”型；一旦有商家发，基本不会漏。

**B. 存储 / 黑五** —— 更贴合 Offers 的日常节奏，适合捡便宜大盘

```dotenv
KEYWORDS=storage box,storage vps,hdd,black friday
EXCLUDE_KEYWORDS=wtb,looking for,request
```

当前命中约 9 条，噪音比直接用 `storage` 或 `€` 小很多（`storage` 会连 NVMe 套餐一起匹配，`€` 会命中几乎所有欧洲商家）。

`.env.example` 默认用 B；想换 A 只改 `KEYWORDS` / `EXCLUDE_KEYWORDS` 两行，重启服务即可，不会补发历史帖子。

## 通知网关对接

请求与网关文档一致：

```http
POST {NOTIFY_URL}
Authorization: Bearer {NOTIFY_TOKEN}
Content-Type: application/json
```

每轮发送的 payload 形如：

```json
{
  "source": "let-offers",
  "title": "LET Offers 新帖 2 条",
  "content": "LowEndTalk Offers\n关键词: storage\n\n1. ...\n   https://...\n\n2. ...",
  "level": "success",
  "data": { "total": 2, "success": 2, "failed": 0 }
}
```

- 网关的 `level` 只有 `success / partial / failed`，这里固定用 `success`（它表示“这批抓取完成”，不是 VPS 续期结果）。
- 不传 `channel` 时默认两个通道都要，最终仍会和网关项目的开关取交集。
- `title` ≤ 200 字，`content` 控制在 5000 字以内，代码里已截断。
- 401/403/400 不重试；429 和 5xx 会自动退避重试 3 次。

在网关后台：

1. 新建一个项目，例如 `let-offers`；
2. 按需打开邮件 / Telegram；
3. 复制详情页的 `NOTIFY_URL` 和 `NOTIFY_TOKEN`，填到 ecloudserv 的环境变量里。

## 部署到 ecloudserv

### 1. 推到公开仓库

ecloudserv 的 Git 部署目前只支持公开仓库，且免费版只有 **1 个 Git 项目**。

```bash
git init
git add .
git commit -m "LET Offers monitor for notify-gateway"
git branch -M main
git remote add origin git@github.com:<你>/let-offers-monitor.git
git push -u origin main
```

不要把 `.env`、`state.json` 提交进去（`.gitignore` 已处理）。

### 2. 建项目

面板 → **Projets → Nouveau projet**：

| 字段 | 值 |
|---|---|
| 仓库 | `<你>/let-offers-monitor` |
| 分支 | `main` |
| 容器语言 | Python |
| 安装命令 | 留空（无依赖） |
| 构建命令 | 留空 |
| 启动命令 | `python main.py` |

### 3. 配环境变量

在容器的 **Variables / Secrets** 里至少填：

```dotenv
NOTIFY_URL=https://<你的-worker>/api/notify
NOTIFY_TOKEN=<项目 Key>
KEYWORDS=cn2,9929,cmin2,japan,tokyo,osaka,storage,hdd,€
EXCLUDE_KEYWORDS=wtb,looking for
MATCH_FIELDS=title
INTERVAL_MIN=10
MAX_PER_RUN=10
SOURCE=let-offers
BOOTSTRAP=skip
STATE_FILE=state.json
```

`PORT` 由平台注入，健康检查走 `GET /health`。这个 worker 不需要绑定域名。

### 4. 首次上线

默认 `BOOTSTRAP=skip`：第一次运行会把 Offers 现有的约 100 条全部记为已读，**不发通知**。之后只推新帖，避免一上线就被旧帖刷屏。

验证顺序：

1. 面板终端执行 `python main.py --test-notify`，看邮件 / Telegram 是否收到测试消息；
2. `python main.py --once --dry-run`，确认关键词能命中（会打印标题和链接）；
3. 看日志出现 `sent N item(s), taskId=...` 即成功；
4. 想立刻推送当前命中，可临时设 `BOOTSTRAP=notify`、`MAX_PER_RUN=5` 重启一次，再改回 `skip`。

### 5. 资源占用

| 项 | 大约 |
|---|---|
| 内存 | 30–50 MB |
| 磁盘 | 代码 < 100 KB，`state.json` 几十 KB |
| 出站 | 每 10 分钟 1 次 RSS（约 200 KB）+ 有命中时 1 次网关请求 |

免费版 512 MB 完全够用，还可以在另一个容器里放别的东西。

## 运维与排查

| 现象 | 处理 |
|---|---|
| `gateway HTTP 401` | `NOTIFY_TOKEN` 错、被轮换，或复制时多了空格 |
| `gateway HTTP 403` | 网关后台把项目关了 |
| `gateway HTTP 429` | 触发网关限流，代码会按 `Retry-After` 退避；检查是否有别的项目共用 Key |
| `gateway HTTP 400` | payload 字段超长；看日志里的 `errors` |
| `feed HTTP 403` | LowEndTalk / Cloudflare 风控；保持 ≥10 分钟间隔，不要频繁重试 |
| 一直没消息 | 先 `--once --dry-run` 看是否命中；再看网关后台「任务」页有没有记录 |
| `state.json` 丢了 | 默认 bootstrap 会重新标记现有帖子，不会补发旧帖，也不会刷屏 |

修改 `KEYWORDS` 不会追溯历史帖子，只影响之后抓到的新帖；改完环境变量后重启服务生效。

## 文件

```text
main.py            # worker
tests/test_main.py # 解析、关键词、payload、网关、bootstrap 测试
.env.example       # 环境变量模板
requirements.txt   # 仅注释：无第三方依赖
```
