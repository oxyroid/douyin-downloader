# Douyin Downloader — Docker HTTP API + Immich/Telegram 集成

基于 [douyin-downloader V2.0](./app/README.md) 封装的 **Docker 化 HTTP 服务**，提供 RESTful API 接口用于下载抖音视频/图文，支持自动上传到 [Immich](https://immich.app/) 自建相册和 [Telegram](https://telegram.org/) Channel，并可通过 iOS 快捷指令一键触发。

## 功能特性

- **Docker 一键部署** — `docker compose up -d` 即可运行
- **HTTP API** — FastAPI 提供 REST 接口，支持异步/同步下载
- **iOS 快捷指令** — GET 接口 + `sync=1` 参数，分享链接即可触发下载并收到通知
- **Immich 自动上传** — 下载完成后自动上传到 Immich，按作者分相册（`douyin-作者名`）
- **Telegram 自动推送** — 下载完成后自动发送到 Telegram Channel/Group
  - 同一作品的封面+视频合并为 MediaGroup（封面在前，视频在后）
  - 自动附带标题、标签和原视频链接
  - 视频自动携带宽高和缩略图，正确显示比例
  - 静音发送，不打扰频道订阅者
  - 支持自建 Bot API Server（local 模式），上传文件最大 2GB
- **三层去重** — API 层（URL 去重）→ 下载器层（SQLite + 本地文件）→ Immich 层（checksum）
- **全部可配置** — 所有参数均可通过 `config.yml` 或环境变量配置

## 项目结构

```
├── app/                        # 应用代码
│   ├── server.py               # FastAPI HTTP 服务
│   ├── immich_uploader.py      # Immich 上传模块
│   ├── telegram_uploader.py    # Telegram 推送模块
│   ├── config.example.yml      # 配置模板
│   └── ...                     # 下载器核心模块
├── telegram-bot-api/           # 自建 Telegram Bot API Server
│   ├── Dockerfile              # 基于 aiogram/telegram-bot-api + proxychains
│   └── entrypoint.sh           # 启动脚本（代理配置 + DNS 解析）
├── Dockerfile
├── docker-compose.yml
├── .env.example                # 环境变量模板
└── .gitignore
```

## 快速开始

### 1. 准备配置

```bash
# 复制配置模板
cp app/config.example.yml app/config.yml

# 复制环境变量模板
cp .env.example .env
```

### 2. 获取 Cookies

抖音下载需要有效的 Cookies。推荐使用自动方式获取：

```bash
cd app
pip install -r requirements.txt
pip install playwright
python -m playwright install chromium
python -m tools.cookie_fetcher --config config.yml
```

在浏览器中登录抖音后，回到终端按回车，Cookies 会自动写入 `config.yml`。

### 3. 配置 Immich（可选）

如果你有自建的 Immich 实例，可以在 `config.yml` 中启用自动上传：

```yaml
immich:
  enabled: true
  api_url: 'http://localhost:2283'   # Immich 地址
  api_key: 'your_api_key_here'       # Immich → 用户设置 → API Keys
```

或者通过 `.env` 文件配置（`config.yml` 优先，环境变量作为备用）：

```bash
IMMICH_API_KEY=your_api_key_here
```

### 3.5 配置 Telegram（可选）

如果你想将下载的内容自动推送到 Telegram Channel/Group：

1. 在 Telegram 中找 [@BotFather](https://t.me/BotFather) 创建 Bot，获取 `bot_token`
2. 将 Bot 添加到目标 Channel/Group 并设为管理员
3. 在 `config.yml` 中配置：

```yaml
telegram:
  enabled: true
  bot_token: '123456:ABC-DEF...'     # Bot Token
  chat_id: '@my_channel'             # Channel 用户名或 chat_id (如 -100xxxx)
  api_base: 'http://telegram-bot-api:8081'  # 使用自建 Bot API Server
  caption_template: '**{author}:** {desc} {tags}'  # 支持 Markdown 风格加粗/斜体
  send_cover: true                   # 是否同时发送封面图
```

或者通过环境变量配置：

```bash
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=@my_channel
```

**自建 Bot API Server（推荐）**

项目内置了 `telegram-bot-api` 容器，基于 `aiogram/telegram-bot-api` + `proxychains-ng` 构建，支持：

- **local 模式**：上传文件最大 2GB（官方 API 限制 50MB）
- **代理支持**：通过宿主机 SOCKS5 代理（默认 `host.docker.internal:7890`）访问 Telegram DC

使用前需在 `.env` 中配置 Telegram API 凭据（从 [my.telegram.org](https://my.telegram.org) 获取）：

```bash
TELEGRAM_API_ID=your_api_id
TELEGRAM_API_HASH=your_api_hash
```

如果不需要自建 Bot API Server，可以将 `api_base` 改为 `https://api.telegram.org` 并移除 `docker-compose.yml` 中的 `telegram-bot-api` 服务。

### 4. 启动服务

```bash
docker compose up -d
```

验证服务是否正常：

```bash
curl http://localhost:8000/health
# {"status": "ok", "immich_enabled": true, "telegram_enabled": false}
```

## API 接口

所有下载接口返回统一的响应格式，包含 `summary` 字段便于展示。

### GET `/d` — 快捷下载（推荐）

最简单的下载接口，适合 iOS 快捷指令或浏览器直接调用。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | ✅ | 抖音链接（需 URL 编码） |
| `sync` | bool | ❌ | `1`=同步等待完成，`0`=异步后台执行（默认） |

```bash
# 异步下载
curl "http://localhost:8000/d?url=https%3A%2F%2Fv.douyin.com%2Fxxxxxxxx"

# 同步下载（等待完成后返回结果）
curl "http://localhost:8000/d?url=https%3A%2F%2Fv.douyin.com%2Fxxxxxxxx&sync=1"
```

**响应示例：**

```json
{
    "task_id": "a1b2c3d4e5f6",
    "status": "completed",
    "url": "https://v.douyin.com/xxxxxxxx",
    "message": "成功 1 / 失败 0 / 跳过 0 | Immich: 上传 2, 重复 0, 失败 0 | Telegram: 发送 2, 跳过 0, 失败 0",
    "summary": "1个下载成功\n2个已上传Immich\n2个已发送Telegram"
}
```

### POST `/download` — JSON 下载

支持更多参数的下载接口，通过 `sync` 字段控制同步/异步。

```bash
# 异步下载
curl -X POST http://localhost:8000/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://v.douyin.com/xxxxxxxx"}'

# 同步下载
curl -X POST http://localhost:8000/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://v.douyin.com/xxxxxxxx", "sync": true}'
```

**请求体参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | ✅ | 抖音链接 |
| `sync` | bool | ❌ | 是否同步等待完成（默认 `false`） |
| `mode` | string[] | ❌ | 下载模式，如 `["post"]` |
| `number_post` | int | ❌ | 下载数量限制，`0` = 全部 |
| `thread` | int | ❌ | 并发数 |

### GET `/task/{task_id}` — 查询任务状态

```bash
curl http://localhost:8000/task/a1b2c3d4e5f6
```

### GET `/health` — 健康检查

```bash
curl http://localhost:8000/health
```

### GET/POST `/reset` — 重置下载记录

清理下载目录、数据库记录和内存缓存，使下次请求时重新下载并上传到 Immich。适用于在 Immich 中手动删除文件后需要重新上传的场景。

```bash
curl http://localhost:8000/reset
```

**响应示例：**

```json
{
    "status": "ok",
    "removed_dirs": 3,
    "removed_files": 1,
    "db_cleared": true,
    "summary": "已清理 3个目录 + 1个文件\n数据库记录已清空\n下次请求将重新下载并上传到Immich"
}
```

## iOS 快捷指令配置

创建快捷指令，在抖音分享页面一键下载并上传到 Immich：

1. **新建快捷指令**，添加「接收输入」动作
2. **添加「获取 URL 内容」动作**（不是「打开 URL」）
   - URL：`https://your-domain.com/d?url=分享输入&sync=1`
   - 方法：GET
3. **添加「获取词典值」动作**
   - 从「URL 内容」获取键 `summary` 的值
4. **添加「显示通知」动作**
   - 标题：抖音下载
   - 内容：上一步的 `summary` 值

> 通过 Cloudflare Tunnel 等方式暴露服务后，可在外网使用。
>
> 如果在 Immich 中删除了文件后需要重新上传，先调用 `/reset` 清理下载记录，再重新分享链接即可。

## 配置说明

### `config.yml` 主要配置项

```yaml
# ── 下载器核心 ──────────────────
path: ./Downloaded/              # 下载目录
thread: 5                        # 并发下载数
retry_times: 3                   # 失败重试次数
database: true                   # 启用 SQLite 去重

# ── HTTP 服务 ──────────────────
server:
  host: 0.0.0.0                  # 监听地址
  port: 8000                     # 监听端口

# ── Immich 集成 ────────────────
immich:
  enabled: true                  # 是否启用 Immich 上传
  api_url: ''                    # Immich API 地址（留空则读环境变量 IMMICH_API_URL）
  api_key: ''                    # Immich API Key（留空则读环境变量 IMMICH_API_KEY）
  album_prefix: 'douyin-'        # 相册名前缀（按作者分相册：douyin-作者名）
  device_id: 'douyin-downloader' # Immich 设备标识
  upload_timeout: 600            # 单文件上传超时（秒）
  upload_extensions:             # 上传的文件类型白名单
    - .mp4
    - .jpg
    # ... 更多格式见 config.example.yml

# ── Telegram 集成 ──────────────
telegram:
  enabled: false                 # 是否启用 Telegram 推送
  bot_token: ''                  # Bot Token（留空则读环境变量 TELEGRAM_BOT_TOKEN）
  chat_id: ''                   # Channel/Group ID（留空则读环境变量 TELEGRAM_CHAT_ID）
  api_base: 'http://telegram-bot-api:8081'  # 自建 Bot API Server（或 https://api.telegram.org）
  caption_template: '**{author}:** {desc} {tags}'  # 支持 **加粗** 和 _斜体_，自动转 HTML
  send_cover: true               # 是否同时发送封面图
  upload_timeout: 600            # 单文件上传超时（秒）
```

### 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `DY_CONFIG_PATH` | 配置文件路径 | `config.yml` |
| `IMMICH_API_URL` | Immich API 地址（config.yml 优先） | — |
| `IMMICH_API_KEY` | Immich API Key（config.yml 优先） | — |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token（config.yml 优先） | — |
| `TELEGRAM_CHAT_ID` | Telegram Channel/Group ID（config.yml 优先） | — |
| `TELEGRAM_API_ID` | 自建 Bot API Server 所需，从 my.telegram.org 获取 | — |
| `TELEGRAM_API_HASH` | 自建 Bot API Server 所需，从 my.telegram.org 获取 | — |

### `docker-compose.yml` 说明

```yaml
volumes:
  - ./downloads:/app/Downloaded     # 下载文件持久化
  - ./app/config.yml:/app/config.yml:ro  # 配置文件挂载
```

修改 `config.yml` 后需重启容器生效：

```bash
docker compose restart
```

## 去重机制

三层去重确保不会重复下载和上传：

| 层级 | 机制 | 粒度 | 持久性 |
|------|------|------|--------|
| **API 层** | 内存中 URL → task_id 映射 | URL 级别 | 容器重启后重置 |
| **下载器层** | SQLite + 本地文件检测 | aweme_id 级别 | 持久化（可通过 `/reset` 清除） |
| **Immich 层** | 文件 checksum 校验 + 垃圾箱恢复 | 文件内容级别 | 持久化（Immich 数据库） |

## 本地开发

```bash
cd app
pip install -r requirements.txt
pip install fastapi uvicorn[standard]

# 直接运行
python server.py

# 或使用 uvicorn（支持热重载）
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

## 致谢

- 核心下载器基于 [jiji262/douyin-downloader](https://github.com/jiji262/douyin-downloader) V2.0
- 照片管理使用 [Immich](https://immich.app/) 自托管方案
- Telegram Bot API 文档: [core.telegram.org/bots/api](https://core.telegram.org/bots/api)