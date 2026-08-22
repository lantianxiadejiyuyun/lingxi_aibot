# 灵犀 详细规划方案

> 版本：v1.1 · 状态：✅ **已按本方案实施完成**（2026-08-17），与规划差异：
> - 数据库按需求改为 **MySQL**（原规划 SQLite）
> - 通知渠道 **Server酱 + 飞书** 已实现（可插拔架构），另有站内通知
> - **定时任务管理页**（内置任务配置 + 自定义 cron 提醒 + 手动执行）已实现
> - AI Function Calling **63 个工具** 覆盖日历/任务/笔记/网页/图片/技能/记忆/语义检索/联网搜索/健身/出行/消费
>
> **后续增量（2026-08，不在本规划内）**：多用户数据隔离、健身、出行、消费 + App REST、`ADMIN_ENTRY` 安全入口。以 [PROJECT.md](PROJECT.md) 与 [README.md](../README.md) 为准。
>
> 使用与部署见仓库根目录 `README.md`；冒烟测试 29/29 通过。

---

## 1. 项目概述

### 1.1 目标

构建一个**单用户优先**的 AI 个人助理基座，把「日程、任务、笔记、习惯」等
工作生活数据统一管理，并以 AI 为贯穿各模块的"大脑"：

- 通过自然语言对话完成管理操作（查日程、建任务、记笔记）
- 每天早上主动汇报今日安排，晚上复盘当天完成情况
- 到点提醒、主动催办，让个人管理从"人找工具"变为"AI 找人"

### 1.2 关键约束（已确认）

| 决策点 | 选择 |
|---|---|
| 技术栈 | Python + Flask（后端与前端渲染一体） |
| AI 服务 | DeepSeek / 国内 API（OpenAI 兼容协议） |
| 使用环境 | 云服务器公网访问（需 HTTPS 与安全加固） |
| 数据规模 | 单用户个人数据（SQLite 足够） |

### 1.3 设计原则

1. **简单优先**：不引入微服务、消息队列等重组件，个人工具应易维护、易备份。
2. **AI 可插拔**：模型服务通过 OpenAI 兼容协议抽象，随时可换 DeepSeek/通义/Kimi/Ollama。
3. **数据自有**：全部数据存于本地 SQLite 文件，备份即复制一个文件。
4. **工具即扩展**：AI 的能力由"工具注册表"决定，新增技能只需注册一个函数。

---

## 2. 技术选型

| 层 | 选型 | 理由 / 备选 |
|---|---|---|
| Web 框架 | Flask 3.x | 用户指定；轻量、生态成熟 |
| 模板 | Jinja2（Flask 内置） | 服务端渲染，配合 HTMX 局部刷新 |
| 前端交互 | HTMX + 原生 JS（可选 Alpine.js） | 无需前端构建链，保持"简单后台" |
| ORM / 迁移 | SQLAlchemy 2.x + Flask-Migrate (Alembic) | 结构变更可迁移 |
| 数据库 | SQLite（WAL 模式） | 单文件零运维；数据量大后再迁 PostgreSQL |
| 表单 / CSRF | Flask-WTF | 自带 CSRF 防护 |
| 登录 / 限流 | Flask-Login + Flask-Limiter | 会话认证 + 防暴力破解 |
| 安全头 | Flask-Talisman | 一键 HTTPS/HSTS/CSP 等安全头 |
| 定时任务 | APScheduler | 提醒、简报、备份；进程内即可，无需 Celery |
| AI 客户端 | openai SDK（自定义 base_url） | 兼容 DeepSeek 等所有 OpenAI 协议服务 |
| 生产服务 | waitress（Windows）/ gunicorn（Linux 容器） | Flask 开发服务器仅限开发 |
| 反向代理 | Caddy | 自动申请/续期 HTTPS 证书，配置极简 |
| 部署 | Docker Compose（推荐）或裸机 venv | 见第 10 节 |
| 测试 | pytest + Flask test client | Phase 0 起建立测试习惯 |

---

## 3. 功能规划（P0 / P1 / P2）

### P0 — MVP（先跑起来，无 AI 也完整可用）

| 功能 | 说明 | 验收标准 |
|---|---|---|
| 用户认证 | 单用户密码登录、退出 | 未登录访问任何页面均跳转登录 |
| 仪表盘 | 今日概览：今日事件、待办任务、习惯状态 | 打开首页即见今日全景 |
| 日历 | 日/周/月三视图；事件增删改；全天事件；重复事件（每日/每周/每月/每年） | 可完整管理一个月行程 |
| 任务 | 待办增删改、优先级、截止时间、完成勾选 | 任务状态流转顺畅 |
| 设置 | 修改密码、时区、AI 参数、简报时间 | 配置持久化并即时生效 |

### P1 — AI 核心（基座的价值所在）

| 功能 | 说明 | 验收标准 |
|---|---|---|
| AI 对话 | 多轮对话、SSE 流式输出、会话历史 | 打字机式输出，历史可回看 |
| 工具调用 | AI 经 Function Calling 操作日历/任务/笔记 | "帮我周三下午加个和客户的会"能真实建出事件 |
| 危险操作确认 | 删除类操作 AI 先向用户确认再执行 | 不会出现 AI 擅自删数据 |
| 事件/任务提醒 | 到期提醒，多渠道推送 | 提前 N 分钟收到通知 |
| 早安简报 | 每日定时：今日日程 + 待办 + 习惯 + AI 建议 | 每天 7:00 自动生成 |
| 晚间复盘 | 每日定时：当天完成情况总结 | 每天 21:00 自动生成 |
| 通知渠道 | 站内通知 + 邮件（可选 Server酱/Telegram） | 至少站内 + 一种外发渠道 |

### P2 — 进阶（按需迭代）

| 功能 | 说明 |
|---|---|
| 笔记 + 全文检索 | SQLite FTS5 检索，作为 AI 长期记忆素材 |
| 习惯打卡 | 周期习惯、连续天数、热力图 |
| 统计看板 | 任务完成率、时间分布、习惯趋势 |
| 导出备份 | 事件导出 iCal，数据导出 JSON/CSV，一键备份 |
| 移动端体验 | 响应式布局 / PWA，手机浏览器可用 |
| 用量统计 | Token 消耗、AI 调用次数仪表 |

---

## 4. 系统架构

```
                        ┌─────────────────────────────────────────┐
   浏览器 (HTMX + JS)   │                Flask 应用                │
   ────────────────►   │  ┌─────────────┐  ┌───────────────────┐  │
        HTTPS          │  │ 蓝图层       │  │  AI 层             │  │
   Caddy 反向代理 ─────►  │  │ auth        │  │  LLM Provider     │  │
                        │  │ dashboard   │  │  (OpenAI 兼容)    │  │
                        │  │ calendar    │  │  Tool Registry    │  │
                        │  │ tasks       │  │  Prompts / Memory │  │
                        │  │ chat (SSE)  │  │  Briefing 流水线   │  │
                        │  │ notes       │  └─────────┬─────────┘  │
                        │  │ settings    │            │            │
                        │  └──────┬──────┘            │            │
                        │  ┌──────▼──────┐   ┌───────▼────────┐   │
                        │  │ 服务层       │   │ 调度层           │   │
                        │  │ services/*  │   │ APScheduler     │   │
                        │  └──────┬──────┘   │ 提醒/简报/备份    │   │
                        │  ┌──────▼──────┐   └─────────────────┘   │
                        │  │ 模型层       │                         │
                        │  │ SQLAlchemy  │                         │
                        │  └──────┬──────┘                         │
                        └─────────┼────────────────────────────────┘
                                  ▼
                        ┌──────────────────┐      ┌──────────────┐
                        │  SQLite (WAL)    │      │ DeepSeek API │
                        └──────────────────┘      └──────────────┘
```

分层职责：

- **蓝图层**：路由、表单校验、模板渲染，不做业务逻辑
- **服务层**：业务逻辑（事件冲突检测、任务状态流转、简报生成等）
- **模型层**：数据实体与查询
- **AI 层**：模型调用、工具执行、提示词与记忆管理
- **调度层**：所有定时任务，与 Web 请求解耦

---

## 5. 目录结构

```
灵犀/
├── app/
│   ├── __init__.py            # app 工厂（create_app）
│   ├── config.py              # 配置类，从环境变量/.env 加载
│   ├── extensions.py          # db / migrate / login / scheduler / limiter
│   ├── models/
│   │   ├── __init__.py
│   │   ├── user.py            # User
│   │   ├── event.py           # Event（日历）
│   │   ├── task.py            # Task
│   │   ├── note.py            # Note
│   │   ├── habit.py           # Habit / HabitLog
│   │   ├── conversation.py    # Conversation / Message
│   │   └── notification.py    # Notification / Setting
│   ├── blueprints/
│   │   ├── auth.py            # 登录/退出/改密
│   │   ├── dashboard.py       # 今日概览
│   │   ├── calendar.py        # 日历视图与事件 API
│   │   ├── tasks.py           # 任务 CRUD
│   │   ├── chat.py            # 对话页 + SSE 流式接口
│   │   ├── notes.py           # 笔记（P2）
│   │   └── settings.py        # 配置页
│   ├── ai/
│   │   ├── llm.py             # LLMProvider 抽象 + OpenAICompat 实现
│   │   ├── registry.py        # 工具注册表（装饰器注册）
│   │   ├── tools/             # 各工具实现：calendar_tools.py 等
│   │   ├── prompts.py         # 系统提示词模板
│   │   ├── memory.py          # 短期窗口 + 长期记忆检索
│   │   └── briefing.py        # 早安简报/晚间复盘流水线
│   ├── services/
│   │   ├── calendar_service.py
│   │   ├── task_service.py
│   │   └── notify_service.py  # 站内/邮件/Server酱/Telegram
│   ├── scheduler.py           # APScheduler 任务注册
│   ├── templates/             # Jinja2 模板（按蓝图分目录）
│   └── static/                # CSS / JS / 图标
├── migrations/                # Flask-Migrate 迁移脚本
├── tests/                     # pytest 测试
├── data/                      # SQLite 数据文件（gitignore）
├── docker/
│   ├── Dockerfile
│   └── caddy/Caddyfile
├── docker-compose.yml
├── requirements.txt           # 或 pyproject.toml
├── .env.example               # 环境变量样例（真实 .env 不入库）
├── run.py                     # 开发入口
└── README.md
```

---

## 6. 数据库设计

> 约定：所有时间字段统一存 **UTC**，展示时按用户时区转换；字段 `*_utc` 后缀明确标识。

### 6.1 表结构

**users**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| username | TEXT UNIQUE | 登录名 |
| password_hash | TEXT | Werkzeug 哈希（scrypt/pbkdf2） |
| timezone | TEXT | 如 `Asia/Shanghai` |
| prefs | TEXT(JSON) | 偏好：简报时间、提醒方式、语言等 |
| created_at | DATETIME | |

**events（日历事件）**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| title | TEXT | |
| description | TEXT | 可空 |
| start_utc / end_utc | DATETIME | 结束可空（瞬时事件） |
| all_day | BOOL | |
| rrule | TEXT | 重复规则（RFC 5545 字符串） |
| reminder_minutes | INTEGER | 提前提醒分钟数 |
| created_at / updated_at | DATETIME | |

**tasks**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| title / notes | TEXT | |
| due_utc | DATETIME | 可空 |
| priority | INTEGER | 1 低 / 2 中 / 3 高 |
| status | TEXT | `open` / `done` / `cancelled` |
| project / tags | TEXT | 简单分组，个人工具不做多级项目树 |
| completed_at | DATETIME | |
| created_at / updated_at | DATETIME | |

**notes**

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| title / content | TEXT | |
| tags | TEXT | |
| created_at / updated_at | DATETIME | |

> 检索：建 FTS5 虚拟表（`notes_fts`），通过触发器同步，支持中文分词按需引入 simple/jieba。

**habits / habit_logs**（P2）

| 表 | 字段 |
|---|---|
| habits | id, name, schedule(JSON 周期), color, archived |
| habit_logs | id, habit_id FK, date, done, note |

**conversations / messages**

| 表 | 字段 |
|---|---|
| conversations | id, title, created_at, updated_at |
| messages | id, conversation_id FK, role(user/assistant/tool), content, tool_calls(JSON), created_at |

**notifications**

| 表 | 字段 |
|---|---|
| notifications | id, type, title, body, channel, status(pending/sent/failed), created_at, sent_at |

**settings**

| 表 | 字段 |
|---|---|
| settings | key TEXT PK, value TEXT(JSON) |

### 6.2 设计要点

- **重复事件**：存储 rrule 规则，查询时按日期范围展开（`dateutil.rrule`），不预生成实例；
  对单次"例外"（改期/取消某次）用 `exceptions` 表记录（P1 后期再做，MVP 只支持整体重复）。
- **时区**：入库一律 UTC，展示按用户 `timezone` 换算，避免跨时区/夏令时混乱。
- **软删除**：任务/事件建议加 `deleted_at`，AI 误操作可回滚（配合危险操作确认）。
- **索引**：`events(start_utc, end_utc)`、`tasks(due_utc, status)`、`messages(conversation_id, created_at)`。

---

## 7. AI 层设计（基座核心）

### 7.1 模型接入抽象

```python
# 概念接口（示意，非最终代码）
class LLMProvider:
    def chat_stream(self, messages, tools=None) -> Iterator[Chunk]: ...
    def chat(self, messages, tools=None) -> AssistantMessage: ...
```

- 默认实现 `OpenAICompatProvider`：`base_url + api_key + model` 全部来自配置
- 环境变量：`LLM_BASE_URL`（DeepSeek: `https://api.deepseek.com/v1`）、
  `LLM_API_KEY`、`LLM_MODEL`（默认 `deepseek-chat`）
- 切换模型 = 改 `.env` 三个变量，代码零改动

### 7.2 工具注册表（基座的扩展机制）

```python
# 概念示意
@register_tool(
    name="create_event",
    description="在日历中创建事件",
    parameters={"type": "object", "properties": {...}},
    confirm_required=False,   # 危险操作置 True
)
def create_event(title, start, end=None, ...): ...
```

首批工具（P1）：

| 工具 | 说明 | 需确认 |
|---|---|---|
| list_events | 查询某时间段日程 | 否 |
| create_event / update_event | 创建/修改事件 | 否 |
| delete_event | 删除事件 | ✅ |
| list_tasks / create_task / update_task | 任务查询与维护 | 否 |
| complete_task | 完成任务 | 否 |
| delete_task | 删除任务 | ✅ |
| add_note / search_notes | 记笔记 / 检索笔记 | 否 |
| get_today_summary | 今日日程+任务汇总 | 否 |
| get_user_prefs / set_user_prefs | 读写偏好（时区等） | 否 |

要点：

- **工具参数与实体字段对齐**，AI 传错时给出友好错误信息让模型自我修正
- **危险操作二次确认**：需要确认的工具先返回"待确认"状态，用户在前端点确认后才真正执行
- **时间解析兜底**：AI 传相对时间（"明天下午"）时，服务层统一用用户时区解析，不依赖模型算 UTC

### 7.3 提示词设计

系统提示词（`prompts.py`）包含：

1. 人设：专业、简洁的中文私人助理
2. **上下文注入**：当前时间、用户时区、今天的日期
3. 工具使用规则：优先用工具查事实，不臆造日程
4. 安全规则：删除类操作必须经用户确认
5. 语言偏好：默认中文回复

### 7.4 记忆机制

- **短期**：会话消息历史；超长时滑动窗口 + 早期消息摘要
- **长期**：笔记全文检索（FTS5）按相关性注入提示词；用户偏好存 `settings`
- P2 可升级：笔记向量化（本地 embedding）做语义检索，替换纯关键词

### 7.5 简报流水线（`briefing.py`）

```
收集数据（今日事件/待办任务/习惯/昨日未完成）
   → 组装提示词（含上下文与用户偏好）
   → LLM 生成简报（Markdown）
   → 存入 conversations（可回看）+ 推送通知
```

### 7.6 稳定性与成本

- LLM 调用设置**超时**（流式 60s、非流式 30s）与一次**重试**
- 工具调用失败 → 错误信息回传模型，最多重试 2 轮，避免死循环
- 记录每次调用的 token 用量（`messages` 表存 usage），P2 做成本看板

---

## 8. 定时任务（APScheduler）

| 任务 | 频率 | 说明 |
|---|---|---|
| 事件提醒扫描 | 每分钟 | 检查未来 N 分钟内需提醒的事件，生成通知 |
| 任务到期扫描 | 每 10 分钟 | 到期待办生成催办通知 |
| 早安简报 | 每日 07:00（可配） | 生成并推送 |
| 晚间复盘 | 每日 21:00（可配） | 生成并推送 |
| 数据库备份 | 每日 03:00 | 复制 SQLite 文件到备份目录（保留最近 7 份） |

实现要点：

- 随 Flask 进程启动（`flask run` 时由 CLI 或 `run.py` 触发），**不用 Gunicorn 多 worker**（避免重复调度），或单 worker + `--preload`
- 任务时间从 `settings` 读取，用户可在设置页修改
- 通知发送失败记录到 `notifications.failed`，不中断调度

---

## 9. 安全设计（公网部署重点）

| 项 | 措施 |
|---|---|
| 传输安全 | 仅 HTTPS（Caddy 自动证书 + HSTS）；HTTP 一律 301 跳转 |
| 密码 | Werkzeug `generate_password_hash`（scrypt），禁止明文 |
| 会话 | Cookie 设 `HttpOnly + Secure + SameSite=Lax`；`PERMANENT_SESSION_LIFETIME` 建议 7 天 |
| 暴力破解 | Flask-Limiter：登录接口按 IP 限流（如 5 次/分钟），连续失败锁定 15 分钟 |
| CSRF | Flask-WTF 全局 CSRF 保护 |
| 安全头 | Flask-Talisman（CSP 按需放宽，HTMX 场景注意 `script-src`） |
| 密钥管理 | `SECRET_KEY` 从环境变量注入，`.env` 不入 git |
| API Key | 仅服务端使用，永不进模板/JS |
| 网络暴露 | 应用容器只监听内网端口，公网仅暴露 Caddy 的 443 |
| 升级 | 依赖用 `pip-audit`/Dependabot 定期检查 |
| 备份 | SQLite 每日备份 + 首次部署验证还原流程 |
| 审计 | 记录登录成功/失败日志（时间、IP） |

---

## 10. 部署方案（云服务器）

### 10.1 方案 A：Docker Compose（推荐）

```
云服务器
├── Caddy 容器        ← 公网 80/443，自动 HTTPS，反代 → app:8000
└── app 容器          ← gunicorn(linux) + Flask，监听 8000
        └── 数据卷 ./data:/app/data   ← SQLite 持久化
```

```yaml
# docker-compose.yml 概念示意
services:
  app:
    build: .
    env_file: .env
    volumes: ["./data:/app/data"]
    restart: unless-stopped
  caddy:
    image: caddy:2
    ports: ["80:80", "443:443"]
    volumes: ["./docker/caddy/Caddyfile:/etc/caddy/Caddyfile", "caddy_data:/data"]
```

- 建议绑定域名（Caddy 自动申请证书）；无域名时用 IP + 自签证书（浏览器会告警，不推荐）
- 更新流程：`git pull && docker compose up -d --build`
- 数据迁移/备份：直接操作 `./data` 目录即可

### 10.2 方案 B：裸机

```
Python venv + pip install -r requirements.txt
→ waitress-serve --port=8000 run:app
→ Caddy（系统服务）反代 443 → 127.0.0.1:8000
```

适合轻量服务器（1C1G 足够），与方案 A 效果一致。

### 10.3 环境变量清单（.env.example）

```
SECRET_KEY=<随机 50 字符>
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-xxxx
LLM_MODEL=deepseek-chat
DATABASE_PATH=/app/data/aibot.db
TZ=Asia/Shanghai
# 通知渠道（可选）
SMTP_HOST= / SMTP_USER= / SMTP_PASS=
```

---

## 11. 实施路线图

| 阶段 | 内容 | 交付物 | 预估 |
|---|---|---|---|
| **Phase 0** | 项目骨架、配置加载、数据库模型+迁移、登录认证、基础布局 | 可登录的空壳应用 + 测试通过 | 1-2 天 |
| **Phase 1** | 日历 CRUD+三视图、任务 CRUD、仪表盘、设置页 | 无 AI 但完整可用的管理后台 | 2-3 天 |
| **Phase 2** | LLM 抽象、工具注册表、对话页(SSE)、Function Calling 打通 | 能用自然语言管理日程任务 | 2-3 天 |
| **Phase 3** | APScheduler 提醒、通知渠道（站内+邮件） | 到期自动提醒 | 1-2 天 |
| **Phase 4** | 早安简报 + 晚间复盘 | 每日自动 AI 汇报 | 1 天 |
| **Phase 5** | 笔记+FTS 检索、习惯打卡、统计、导出备份 | 完整个人管理体系 | 2-4 天 |
| **Phase 6** | 安全加固、Docker 部署、上线验证 | 公网可访问的正式环境 | 1-2 天 |

每阶段验收：功能验收 + 关键路径 pytest 用例 + 手工冒烟清单。

---

## 12. 关键设计决策与风险

### 12.1 决策记录

| 决策 | 选择 | 理由 | 触发升级的条件 |
|---|---|---|---|
| 数据库 | SQLite | 单用户零运维 | 多用户/高并发写入 → PostgreSQL |
| 前端 | HTMX | 免构建链、开发快 | 交互复杂到难以维护 → Vue 3 |
| 任务队列 | APScheduler 进程内 | 任务少、够用 | 任务多且需可靠重试 → Celery/RQ |
| 重复事件 | rrule 规则 + 查询展开 | 存储小、逻辑标准 | 需复杂单次例外编辑 → 生成实例表 |
| 检索 | FTS5 关键词 | 简单可靠 | 需语义搜索 → 向量库 RAG |

### 12.2 主要风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 时区处理错误 | 提醒错点、简报错日期 | 全链路 UTC 存储 + 显示层转换 + 测试覆盖 |
| AI 误操作数据 | 删错/改错 | 危险操作确认 + 软删除可回滚 |
| LLM 工具调用失败 | 对话卡死/误导 | 错误回传、限轮重试、降级为纯文本回答 |
| 公网被攻击 | 数据泄露 | 第 9 节全部措施 + 定期备份 |
| API 成本失控 | 账单超预期 | 用量统计 + P2 成本看板 |
| 单点故障（服务器宕机） | 服务不可用 | 每日备份，恢复步骤文档化 |

---

## 13. 未来扩展（P3+，暂不实施）

- 🤖 微信/Telegram 机器人桥接：消息通道接入基座
- 🎙️ 语音输入（Whisper API 转写后进对话）
- 🧠 向量 RAG：笔记/文档语义检索，打造私人知识库
- 👥 多用户与家庭共享
- 📱 PWA 移动端：添加到主屏、离线缓存
- 📡 iCal/CalDAV 同步：与手机日历互通

---

## 附：待确认问题（实施前）

1. 服务器操作系统与规格（建议 Ubuntu 22.04+，1C1G 即可）
2. 是否有可用域名（决定 Caddy 证书方式）
3. 通知渠道偏好：邮件 / Server酱 / Telegram 哪个优先
4. UI 语言：中文（默认）是否需要英文切换
