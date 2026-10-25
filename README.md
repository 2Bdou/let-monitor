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

注意逗号是分隔符，正则里不要写逗号（`{1,2}` 这类量词也不行），需要“或”时用 `|`。

**在 ecloudserv 上还有一条**：平台生成 `.env` 时会把反斜杠转义成两个，`\d` `\s` `\/` 到容器里就失效了。所以关键词里的正则不要用反斜杠，用等价写法：

```dotenv
# 不要写 /\$\d\d?\/year/i ，换成：
/[^0-9][0-9][0-9]?([.][0-9][0-9])?/(year|yr)/i
```

### 当前生效的关键词（线路 / 低价 / KVM）

```dotenv
KEYWORDS=cn2,gia,9929,cmin2,cmi,4837,cuvip,hong kong,japan,tokyo,osaka,singapore,korea,seoul,taiwan,kvm,cheap,budget,low cost,lifetime,/[^0-9][0-9][0-9]?([.][0-9][0-9])?/(year|yr)/i,三网,回程,联通,电信
EXCLUDE_KEYWORDS=wtb,looking for,request,dedicated server,object storage,mac mini,email hosting
```

- **线路**：`cn2` `gia` `9929` `cmin2` `cmi` `4837` `cuvip`（中国大陆优化线路），外加 `三网` `回程` `联通` `电信` 兜住中文标题
- **地区**：`hong kong` `japan` `tokyo` `osaka` `singapore` `korea` `seoul` `taiwan`
- **架构**：`kvm`
- **低价**：`cheap` `budget` `low cost` `lifetime`，以及年付价 ≤ 99（带货币符号或空格，`€12.99/year`、`$49/yr` 都算）
- **排除**：收机器/求推荐，以及独立服务器、对象存储、Mac mini、邮件托管这类噪音

2026-10-25 实测：100 条里命中 34 条，其中约 21 条是 KVM VPS；`cn2/9929/cmin2` 当时为 0，属于“等鱼”型，留着不会误报。任一关键词命中即通知（OR）。

### 备选

**A. 只要中国线路 / 亚洲**（更窄，当前约 5 条）

```dotenv
KEYWORDS=cn2,gia,9929,cmin2,cmi,4837,cuvip,japan,tokyo,osaka,hong kong,singapore,taiwan
EXCLUDE_KEYWORDS=wtb,looking for,request,dedicated server,object storage
```

**B. 存储 / 黑五**（当前约 9 条）

```dotenv
KEYWORDS=storage box,storage vps,hdd,black friday
EXCLUDE_KEYWORDS=wtb,looking for,request
```

换哪套只改 `KEYWORDS` / `EXCLUDE_KEYWORDS` 两行再重启，不会补发历史帖子。

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

仓库：<https://github.com/2Bdou/let-monitor>（公开）。

ecloudserv 的容器用的是 Pterodactyl 的 Python 镜像，**不需要**「Projets」里的 Git 部署功能，直接用容器自带的 `Démarrage` + `Variables` 两个标签页就够了。免费版 RAM 总额 512 MB，也就是一个 512 MB 容器。

### 1. 容器「Démarrage」标签页

| 变量 | 值 |
|---|---|
| Git Repo Address (`GIT_ADDRESS`) | `https://github.com/2Bdou/let-monitor` |
| Git Branch (`BRANCH`) | `main` |
| App py file (`PY_FILE`) | `main.py` |
| Auto Update (`AUTO_UPDATE`) | `1` |
| Additional Python packages (`PY_PACKAGES`) | 留空 |
| Git Access Token (`ACCESS_TOKEN`) | 留空（公开仓库不需要） |

平台据此生成启动命令，顺序是：没有 `.git` 就 clone，有就 `git pull` → `pip install -r requirements.txt`（本项目没有依赖，为空操作）→ `python main.py`。改完这些变量后点一次 **Réinstaller**（或直接重启）让代码落地。

镜像用 Python 3.12 即可，代码兼容 3.8+。

### 2. 容器「Variables」标签页

这里加的变量会**自动写进容器里的 `.env`**（文件头写着「Ne modifie pas ce fichier à la main」），代码启动时用 `load_dotenv()` 读。至少填：

```dotenv
FEED_URL=https://lowendtalk.com/categories/offers/feed.rss
NOTIFY_URL=https://notify.cabin.ccwu.cc/api/notify
NOTIFY_TOKEN=<网关项目 Key，勾选 secret>
KEYWORDS=storage box,storage vps,hdd,black friday
EXCLUDE_KEYWORDS=wtb,looking for,request
MATCH_FIELDS=title
BOOTSTRAP=skip
INTERVAL_MIN=10
MAX_PER_RUN=10
SOURCE=let-offers
STATE_FILE=state.json
```

值里含空格时平台会加引号，`load_dotenv()` 会去掉，不用自己处理。改完变量必须**重启**才生效。

`PORT` 不用管：这个 worker 只在 `0.0.0.0:3000` 开一个 `GET /health`，不需要对外域名，容器也不占公网端口。

### 3. 首次上线

默认 `BOOTSTRAP=skip`：第一次运行会把 Offers 现有的约 100 条全部记为已读，**不发通知**，之后只推新帖。`state.json` 落在 `/home/container`，重启不丢；因为它被 `.gitignore` 忽略，`git pull` 也不会覆盖它。

验证顺序（面板 `Terminal` 里执行）：

```bash
cd /home/container
python main.py --test-notify                # 网关链路，会真发一封测试邮件
DRY_RUN=1 STATE_FILE=/tmp/dry.json python main.py --once --dry-run   # 看关键词命中
curl -s localhost:3000/health               # 常驻进程的健康状态
```

想立刻把当前命中推一次，可把 `BOOTSTRAP` 临时改成 `notify`、`MAX_PER_RUN=5`，重启跑完再改回 `skip`。

### 4. 资源占用（实测）

| 项 | 实测 |
|---|---|
| 内存 | 约 34 MB（`python main.py` 常驻） |
| 磁盘 | 代码 + `.git` 约 85 KB，`state.json` 约 5 KB |
| 出站 | 每 10 分钟 1 次 RSS（约 200 KB）+ 有命中时 1 次网关请求 |

512 MB 容器绰绰有余。建议在容器设置里打开 **auto-restart**，进程异常退出时自动拉起。

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
