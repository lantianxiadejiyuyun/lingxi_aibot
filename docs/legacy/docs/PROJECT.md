# 灵犀 项目文档

> 版本：v1.2 · 更新日期：2026-08-23 · 代码位置：E:\Codes\AiBot（含后台界面分组）
> 相关文档：[README.md](../README.md)（快速上手）· [部署.md](部署.md)（宝塔/服务器/Docker/MySQL）· [开发指南](开发指南.md)（本地开发与测试）· [PLAN.md](PLAN.md)（原始规划）· [使用说明.html](使用说明.html)（面向使用者）

---

## 1. 项目概述

**灵犀** 是一个以 AI 为核心的**个人工作生活管理基座**：统一管理日程、任务、笔记、健身、出行、消费与定时提醒，
通过自然语言对话（AI 可调用已注册的功能工具）、早/午/晚定时简报、多渠道通知（站内 / Server酱 /
飞书 Webhook / 飞书应用机器人），帮助用户更好地安排工作与生活。

| 属性 | 值 |
|---|---|
| 定位 | 私有 AI 助理（基座式设计，能力可插拔扩展）；数据按用户隔离，管理员可创建账号 |
| 形态 | Flask 全栈 Web 后台（服务端渲染 + 轻量 JS，无前端构建链） |
| 存储 | MySQL 5.7+（PyMySQL） |
| AI | 每用户独立 API Key；OpenAI 兼容（chat.completions）与 Anthropic Messages；SSE 流式 + 工具调用 |
| 部署 | 本机开发 / 宝塔 waitress / 普通 Linux systemd / Docker（见 [部署.md](部署.md)） |

---

## 2. 技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| Web 框架 | Flask 3.x | 后端 + Jinja2 前端渲染一体 |
| 前端交互 | 原生 JS + HTMX 风格工具函数 | `static/js/app.js`：JSON API / SSE 流式 / Markdown-lite / 弹窗 / Toast |
| ORM | SQLAlchemy 2.x + Flask-Migrate | 模型驱动，支持迁移 |
| 数据库 | MySQL 5.7+（PyMySQL 驱动） | 时间统一 naive UTC 存储 |
| 表单/安全 | Flask-WTF / Flask-Login / Flask-Limiter | CSRF、会话认证、登录限流（10 次/分） |
| 定时任务 | APScheduler 3.11 | 进程内调度，DB 驱动的任务定义 |
| AI 客户端 | openai SDK（自定义 base_url） | 兼容 DeepSeek / 通义 / Kimi / Ollama |
| 通知 | requests（HTTP 渠道） | 可插拔渠道注册制 |
| 生产服务 | waitress（Windows）/ gunicorn（Docker/Linux） | 单 worker + 多线程（避免调度器重复） |
| 反向代理 | Caddy 2 | 自动 HTTPS 证书 |

---

## 3. 系统架构

```
                        ┌──────────────────────────────────────────────┐
   浏览器 (原生 JS)      │                  Flask 应用                    │
   ───────────────►     │  ┌────────────┐  ┌────────────────────────┐   │
        HTTPS           │  │ 蓝图层      │  │ AI 层                   │   │
   Caddy 反向代理 ─────► │  │ auth       │  │ llm.py（LLM 抽象）       │   │
                        │  │ dashboard  │  │ registry.py（工具注册表）│   │
                        │  │ calendar   │  │ executor.py（对话循环）  │   │
                        │  │ tasks      │  │ prompts / memory        │   │
                        │  │ notes      │  │ briefing.py（三档简报）  │   │
                        │  │ pages      │  └────────────┬────────────┘   │
                        │  │ images     │               │                │
                        │  │ skills     │               │                │
                        │  │ memory     │               │                │
                        │  │ fitness    │               │                │
                        │  │ travel     │               │                │
                        │  │ expenses   │               │                │
                        │  │ chat (SSE) │               │                │
                        │  │ jobs       │               │                │
                        │  │ notifications│             │                │
                        │  │ settings   │  ┌────────────▼────────────┐   │
                        │  │ feishu(回调)│  │ 服务层 services/*        │   │
                        │  │ pages_site │  │ page/image/skill/memory  │   │
                        │  │ image_files│  │ fitness/travel/expense   │   │
                        │  │ expenses_api│ │ notify_service（渠道框架）│   │
                        │  └─────┬──────┘  └────────────┬────────────┘   │
                        │  ┌─────▼──────┐  ┌────────────▼────────────┐   │
                        │  │ 模型层 17 表│  │ 调度层 scheduler.py       │   │
                        │  └─────┬──────┘  │（APScheduler + 动作注册表）│   │
                        │  ┌─────▼──────┐  └─────────────────────────┘   │
                        │  │ utils/     │                                │
                        │  └────────────┘                                │
                        └──────────────────────────────────────────────┘
                                  │                          │
                        ┌─────────▼──────────┐     ┌─────────▼──────────┐
                        │ MySQL（17 张表）    │     │ DeepSeek / 飞书 /   │
                        └────────────────────┘     │ Server酱 / 微信     │
                                                   └────────────────────┘
```

**分层职责**

| 层 | 职责 |
|---|---|
| 蓝图层 | 路由、表单校验、模板渲染，不含业务逻辑 |
| 服务层 | 业务逻辑：日历/任务/笔记/网页/图片/技能/记忆/健身/出行/消费、通知框架、备份、任务管理 |
| 模型层 | 17 张表的 ORM 实体 |
| AI 层 | LLM 调用封装、工具注册与执行、对话循环、提示词、简报流水线 |
| 调度层 | APScheduler 封装：DB 任务定义 → 定时动作执行、手动触发、动态重载 |

---

## 4. 目录结构

```
灵犀/
├── app/
│   ├── __init__.py            # 应用工厂（蓝图注册/工具加载/调度器启动）
│   ├── config.py              # 配置（.env → Config）
│   ├── commands.py            # flask CLI：init-db / create-admin / reset-db / reset-admin-password / backup-now / restore-backup / reindex / routes
│   ├── extensions.py          # db / migrate / login / csrf / limiter 单例
│   ├── scheduler.py           # APScheduler 封装 + @register_action 动作注册表
│   ├── models/                # 17 张表 ORM 模型
│   ├── blueprints/            # 22 个蓝图（主功能 + pages_site / image_files / expenses_api / settings_api / setup）
│   ├── services/              # 业务服务 + 网页/图片/技能/记忆/健身/出行/消费 + 通知渠道（channels/）
│   ├── ai/                    # LLM 抽象 / 工具注册表 / 对话执行器 / 提示词 / 简报 / tools（63 个）
│   ├── templates/             # Jinja2 模板（按蓝图分目录）
│   ├── static/                # style.css / app.js（侧栏分组、设置 Tab 分组、设计令牌）
│   └── utils/                 # timeutil / urlsafety / api_auth / scoping（当前用户上下文）
├── data/backups/              # 备份文件目录（自动生成）
├── scripts/                   # 冒烟测试与功能测试脚本
├── docker/                    # Caddy 配置
├── Dockerfile / docker-compose.yml
├── requirements.txt / .env / .env.example
├── run.py / wsgi.py
└── README.md / docs/{PROJECT,PLAN,部署.md,使用说明.html}
```

---

## 5. 功能模块

### 5.1 页面清单

| 页面 | 路由 | 功能 |
|---|---|---|
| 登录 | `/login` | 密码登录（限流 10 次/分）、CSRF、session 会话 |
| 仪表盘 | `/` | 今日统计（日程/待办/到期/完成）、今日日程、待办列表、快捷操作（日历/任务/笔记/健身/出行/消费/网页/AI）、AI 提示语 |
| 安装引导 | `/setup` | 安装向导始终可打开，每次从第①步开始：①保存数据库连接 → ②开始初始化（有表也不跳过，可重置/DROP 全部表）→ ③创建管理员 → ④基础配置。步进只跟点击，不检测管理员。写接口一律校验安装令牌。其它页面在系统未初始化时仍会跳到 `/setup` |
| 日历 | `/calendar/` | 月视图网格（周一起始、重复展开、今天高亮）、当日明细、事件增删改弹窗 |
| 任务 | `/tasks/` | 状态/优先级/关键词筛选、勾选完成、优先级圆点、标签、到期高亮 |
| 笔记 | `/notes/` | 卡片流、全文搜索、增删改弹窗 |
| 网页 | `/pages/` | 网页生成器：列表（显示开关/复制/删除）、在线编辑器（源码 + 实时预览）、公开链接 |
| 图片 | `/images/` | 图片库：生成、AI 改图、公开/私有开关、下载、删除；`/img/<文件名>` 文件访问 |
| 技能 | `/skills/` | AI 自写技能：人工审核启用/停用、查看代码、删除（新建默认禁用） |
| 记忆 | `/memory/` | 长期记忆：查看/删除 + 手动「立即梳理」上下文 |
| 健身 | `/fitness/` | 训练记录（类型/时长/强度/卡路里/体重）+ 统计看板；内置每日提醒与每周 AI 分析 |
| 出行 | `/travel/` | 出行计划（多段交通/备选方案/预算）+ AI 生成逐日行程；内置出发前一天提醒 |
| 消费 | `/expenses/` | 记账 + 按月筛选 + 分类/月份/支付方式统计；另有 App REST API |
| AI 助手 | `/chat/` | 会话列表 + 流式对话（SSE）+ 工具调用执行痕迹（tool-chip）+ 图片内联显示 |
| 定时任务 | `/jobs/` | 内置任务（改 cron/开关/立即执行）+ 自定义定时提醒（cron）CRUD |
| 通知中心 | `/notifications/` | 渠道/状态筛选、标已读、全部已读、清除已读、删除 |
| 设置 | `/settings/` | Tab 分五组（吸顶）：**账户**（账号、用户管理）· **AI**（模型与人设、语音、联网搜索）· **通知**（渠道、飞书、简报）· **生成**（图片、视觉）· **系统**（备份、网页站点） |

### 5.1.1 后台界面

侧栏由 `register_context` 注入 `nav_groups`（`app/__init__.py`），模板 `base.html` 渲染：

| 分组 | 入口 |
|---|---|
| （置顶） | 仪表盘、AI 助手（`nav-item-ai` 强调） |
| 工作 | 日历、任务、笔记、网页、图片 |
| 生活 | 健身、出行、消费 |
| 智能 | 技能、记忆 |
| 系统 | 定时任务、通知、设置 |

- 顶栏：当前页 Bootstrap Icon + 标题（不再用 emoji）；右侧「AI 助手」按钮（对话页隐藏）、主题切换、用户名、退出
- **界面主题**：浅色 / 深色 / 跟随系统。登录用户写入 `users.prefs.theme`（`POST /settings/api/theme`）；未登录回退 `localStorage`。`<html data-theme>` 在 CSS 加载前由内联脚本设置，避免闪白。设置页「账户 → 外观」同步三选项
- 设置 Tab：`tabs-wrap` 分组 + 胶囊选中态，`position: sticky`；`app.js` 在 `[data-tabs-scope]` 内切换（跨组只高亮一个）
- 窄屏（≤860px）：侧栏收成图标、分组标签隐藏
- 静态资源版本号 `v=20260823c`（`style.css` / `app.js`）

### 5.2 AI 助手（基座核心）

- **对话人设**：设置页「模型与人设 → 对话人设」（用户级 settings：`ai_persona_name` / `ai_persona_preset` / `ai_persona_verbosity` / `ai_persona_address` / `ai_persona_extra` / `ai_persona_ack_template` / `ai_persona_ack_enabled`）。预设：默认助理 / 专业干练 / 温柔陪伴 / 幽默机智 / 讲解老师 / 行动教练 / 自定义。`build_system_prompt` 注入名字与风格，**使用规则始终追加且优先于人设**（避免改人设把工具调用改没）。保存后下一轮对话生效，无需重启。
- **立即回复**：消息一到先发确认再作答。默认模板 `收到：{message}`；占位符 `{message}`（原话，超长截断）、`{name}`、`{address}`。`run_chat` 先 yield `ack`；网页把 ack 转成首段 `delta`；飞书先单独发一条，正文再发并去掉开头确认行。关闭（`ai_persona_ack_enabled=false`）则跳过
- **SSE 流式输出**：`POST /chat/api/send` 返回 `text/event-stream`，事件：`delta`（增量文本）/ `ack`（立即确认，网页转 delta）/ `tool`（工具调用与结果）/ `title` / `done`（含确认行）/ `error`
- **Function Calling**：模型按需调用工具，工具结果回传模型继续推理，最多 8 轮防死循环
- **63 个工具**（`app/ai/tools/`，自动发现注册）：

| 领域 | 工具 |
|---|---|
| 日历 | list_events / create_event / update_event / delete_event |
| 任务 | list_tasks / create_task / update_task / complete_task / delete_task |
| 笔记 | list_notes / create_note / update_note / search_notes |
| 网页 | list_pages / get_page / create_page / update_page / duplicate_page / delete_page |
| 图片 | generate_image / list_images / get_image / edit_image / set_image_public / delete_image |
| 技能 | create_skill / list_skills / get_skill / update_skill / set_skill_enabled / delete_skill / get_skill_template |
| 记忆 | remember / list_memories / delete_memory |
| 语义检索 | semantic_search |
| 联网 | web_search / fetch_page |
| 定时任务 | list_scheduled_jobs / set_job_enabled / update_job_schedule / create_reminder_job / delete_scheduled_job |
| 通知 | send_notification / list_notifications |
| 健身 | list_fitness_records / create_fitness_record / update_fitness_record / delete_fitness_record / analyze_fitness |
| 出行 | list_trip_plans / get_trip_plan / create_trip_plan / update_trip_plan / generate_trip_itinerary / delete_trip_plan |
| 消费 | list_expenses / get_expense_stats / create_expense / update_expense / delete_expense |
| 元信息 | get_today_summary / get_app_status |

- **记忆**：短期 = 最近 20 条消息窗口；长期 = 笔记全文检索 + 用户时区/偏好设置
- **示例**："明天下午 3 点提醒我开会" → `create_event`（含提醒）；"每天 9 点半提醒我喝水" → `create_reminder_job`；"帮我做一个个人主页" → `create_page`；"帮我画一张星空下的柴犬" → `generate_image`；"今天跑步 40 分钟" → `create_fitness_record`；"帮我规划下周去杭州" → `create_trip_plan` + `generate_trip_itinerary`；"午饭 38 微信支付" → `create_expense`

### 5.3 定时简报（三条流水线）

| 简报 | 默认时间 | 内容 |
|---|---|---|
| ☀️ 早安简报 | 07:00 | 今日日程 + 待办任务 + AI 建议 |
| 🕛 午间简报 | 12:00 | 今日剩余日程 + 上午完成 + 待办 + 下午建议 |
| 🌙 晚间复盘 | 21:00 | 今日完成 + 未完成 + 明日安排 |

- 时间在「设置 → 简报」修改，保存后**自动同步内置定时任务 cron**
- LLM 可用时 AI 生成 Markdown；不可用时降级为纯文本列表
- 结果存入同名会话（可回看），并推送通知到配置渠道

### 5.4 飞书机器人（双向，文本 + 图片）

- **收消息接入方式**（settings `feishu_receive_mode`，后台可配）：
  - `callback`（默认）：`POST /feishu/event` HTTP Webhook。URL 验证握手、AES-256-CBC 解密、v1/v2 兼容、message_id 去重；需公网 HTTPS
  - `sdk`：官方 `lark-oapi` WebSocket 长连接（`app/services/feishu_ws.py`），进程主动连开放平台，无需公网回调；开放平台须选「使用长连接接收事件」。保存配置后热启停；HTTP 路由在此模式下忽略消息事件（仍可握手），避免双通道重复回复。**无公网 IPv4 / 未配置网页域名公网 A 记录时，飞书里打不开 AI 构建的网页**（设置页黄条提示；回复含网页地址时附带说明，见 `utils/netinfo.py`）
- **消息处理**（两种接入共用 `feishu_inbound.py`）：文本 → 绑定用户 → 会话 → AI 全工具回复；图片 → 下载入库，配置视觉模型时识别回传
- **回复文本**：收到文本消息 → 自动创建/续接会话（chat_id → conversation 映射，多轮上下文）→ 若开启立即回复则先发确认句 → AI 全工具可用 → 通过 `im/v1/messages` API 再发正文（去掉开头确认行，避免重复）
- **回复图片**：本轮对话中 AI 调用 `generate_image`/`edit_image` 生成图片时，自动经 `im/v1/images` 上传取 `image_key` → `msg_type:image` 把图发回飞书（文本回复自动去掉 Markdown 图片链接，避免显示裸 URL）
- **主动发送**：`feishu_app` 渠道经 tenant_access_token（缓存至过期前 60s）发送文本/图片到指定 chat_id / open_id
- 前置条件：飞书自建应用 + 公网 HTTPS 回调地址（国内需备案域名），见 README

### 5.5 网页生成器（自由 HTML 页面，后台/网页域名分离）

- **生成**：AI 对话调用 `create_page`（模型直接编写完整 HTML，工具自动剥离 Markdown 代码围栏）；管理页手动新建/编辑
- **管理**（`/pages/`）：列表（标题/slug/公开性/显示开关/更新时间）、在线编辑器（左侧源码 + 右侧 iframe srcdoc 实时预览，Tab 缩进、Ctrl+S 保存）、复制、软删除（slug 同步释放）
- **访问控制**：
  - `is_public=true` 任何人可访问；`false` 仅登录用户可见
  - `enabled=false`（后台显示开关）前台一律 404，后台仍可编辑
- **独立 HTTP 端口（推荐，无需 HTTPS）**：设置 `page_port` / `PAGE_PORT`（如 8080）后，`page_site_server` 在本进程再监听一个端口，只提供公开网页 `/<slug>` 与公开图 `/img/`。地址 `http://主机:端口/slug`；主机来自 `PAGE_HOST` 或自动局域网 IPv4。保存后热启停。
- **域名分离（可选高级）**：`admin_domain` / `page_domain` 仍可用于已有 Nginx/Caddy HTTPS 的部署；不配证书时请用端口模式。
- **实现**：`page_service.page_public_url` 优先端口 HTTP，其次域名 HTTPS，最后后台 `/p/<slug>`

### 5.6 图片生成（OpenAI 兼容 images 接口）

- **生成**：`POST {base}/images/generations`，优先请求 `b64_json` 返回（避免外链失效）；端点不支持时自动降级为 `url` 模式并下载落盘；文件存 `data/images/`（文件名 uuid 化），记录存 `image_assets` 表
- **AI 改图**（`edit_image`）：优先 `POST {base}/images/edits`（multipart，OpenAI gpt-image 系列支持）；端点 404/405/不支持模型时**自动降级**为「修改要求 + 原图提示词」重新生成，新图 `parent_id` 关联原图、`instruction` 记录修改要求 —— 修改链路始终可用
- **访问控制**：`/img/<文件名>` 严格文件名白名单（`img-<uuid8>-<ts>.<ext>`）+ DB 记录校验；公开图片任何人可访问（`Cache-Control: public`，供生成的网页嵌入），私有图片仅登录可见（未登录 404）
- **对话内显示**：工具返回 Markdown 图片链接 `![...](/img/xxx.png)`，前端 `mdLite` 渲染为 `<img>`（同时支持单独成行的图片链接）
- **实现**：`models/image.py` + `services/image_service.py` + `blueprints/images.py`（`images` 管理蓝图 + `image_files` 文件蓝图）+ `ai/tools/image_tools.py`（6 工具）；配置 `image_base_url / image_api_key / image_model / image_size`（设置页「图片生成」可改，保存即生效）

### 5.7 自进化能力（技能 + 记忆）

**技能（AI 自写工具）**
- AI 通过 `create_skill` 编写 Python 函数体（name/description/parameters JSON Schema/code），存入 `skills` 表；`get_skill_template` 提供编写规范（参数推导/返回约定/可用环境/示例）
- **受限沙箱 + 子进程执行**（`services/skill_service.py` + `services/skill_worker.py`）：签名由 `parameters.properties` 推导；内置白名单；注入 `db/json/datetime/re` 与各 service；静态扫描拒绝 import/子进程/网络/文件/反射/`while True` 为第一道防线；执行走**独立子进程**，`SKILL_TIMEOUT=10s` 超时强杀（死循环不再挂死主进程）
- **人工审核**：新建默认 `enabled=False`；「🧩 技能」页查看代码 → 启用/停用/删除；应用启动 `load_skills()` 自动加载已启用技能（单个失败跳过）
- **AI 工具**：get_skill_template / create_skill / list_skills / get_skill / update_skill / set_skill_enabled / delete_skill

**RAG 语义检索**
- `models/embedding.py`（`embeddings` 表：source_type/source_id/vector/model）+ `services/rag_service.py`（OpenAI 兼容 `/embeddings`、归一化余弦检索、upsert/删除索引）
- 挂钩：note/page 增改删自动维护；上下文梳理把会话摘要入索引；`flask reindex` 重建
- AI 工具 `semantic_search(query, sources, k)`；未配置嵌入服务（`EMBEDDING_*`）时降级关键词搜索并提示

### 5.9 联网搜索 + 调研工作流

- `services/web_search_service.py`：可插拔提供方 —— `bing`（默认，免 key 国内可用）/ `duckduckgo`（免 key）/ `searxng`（自托管 JSON）/ `serper` / `tavily`（Key）；`fetch_page` 抓取正文（去 script/style/标签）；网络异常统一转 `SearchError`
- AI 工具：`web_search(query, limit)` + `fetch_page(url)`；聊天 `mdLite` 支持 Markdown 链接与裸 URL 渲染（可点击新标签打开）
- **调研工作流**（系统提示词规则 5）：搜索 → `create_page` 生成「📚 调研：主题」调研网页（含结论 + 可点击链接）→ 用网页地址回答；用户要求收录知识库 → `create_note` 提炼核心（tag「调研」）→ 自动进 RAG 知识库
- 设置页「联网搜索」tab：提供方 + 密钥 + 测试搜索

**记忆（定时自动梳理上下文）**
- 会话摘要：`consolidate_conversation` 把长对话（>12 条）压成 `conversations.summary`（保留最近 8 条原文）
- 长期记忆：`extract_memories` 跨对话抽取事实/偏好存 `memories` 表（`source=auto` 每次整体替换，`manual` 手动保留）；每条带 `importance`(1-5) 与 `expires_at`，过期的自动遗忘（`delete_expired` 软删）
- 上下文注入：`build_messages` = system 提示词 + 全局记忆（过滤过期、按重要度降序）+ 会话摘要 + 最近消息（有摘要时窗口 8，否则 20）
- 触发：内置任务 `context_consolidation`（每日 04:00）+ 「🧠 记忆」页手动「立即梳理」
- **AI 工具**：remember（支持重要度/过期）/ list_memories / delete_memory

### 5.8 每周自动整理

- `maintenance_service.run_cleanup()`：① 归档到期超 7 天未完成任务（cancelled）② 同标题笔记合并（内容并入最早一条，其余软删）③ 网页外链坏链体检（并发 HEAD/GET，超时 6s，最多 30 条）④ 删除已读超 30 天通知
- 内置任务 `weekly_cleanup`（每周日 02:00），结果汇总推送通知 + 日志

### 5.10 备份与数据管理（备份 / 导出 / 恢复 / 重置）

**备份（流式 + gzip）**
- `backup_service.backup_to_json()`：逐表、逐行**流式写出**（内存占用与单行相当、与数据总量无关），gzip 压缩为 `.json.gz`（解压即标准 JSON）；配合 MySQL 服务端游标（`stream_results`）与单事务一致性快照，超大表也能稳定备份
- 保留最近 N 份（`backup_keep`，默认 7，可配置 1-30）；`list_backups()` / `delete_backup()` 兼容历史 `.json` 与新的 `.json.gz`
- 触发：内置任务 `data_backup`（每日 03:00，后台线程）+ 手动「立即备份」（`/settings/api/backup-now`）+ `flask backup-now`

**导出下载**：`GET /settings/export` → 即时生成最新备份并直接下载（同时保留在「最近备份」列表）

**恢复（导入备份）**：`POST /settings/restore` —— 支持选择服务器已有备份或上传 `.json/.json.gz`；恢复前自动做一次当前数据的安全备份，逐表清空 + 分批重建（保留原 ID、字符串自动按列类型回写），失败整体回滚；`flask restore-backup <path>` 离线恢复

**一键重置**：`POST /settings/reset` —— 清空全部业务数据（保留账号、系统配置、内置定时任务并重置其运行历史，清理 `data/images`）；重置前自动备份；需当前密码 + 输入「重置」确认

**安全确认**：导出/重置/恢复均需登录；重置/恢复需当前密码 + 确认文字 + 原生确认框三重确认；恢复为全量覆盖（含账号与设置）

> 一键重置会清空健身 / 出行 / 消费三张表（表不存在则跳过）。备份导出全部表。

### 5.11 健身

- **页面** `/fitness/`：训练记录列表 + 统计（次数 / 总时长 / 总卡路里 / 最近体重）+ 弹窗增删改
- **字段**：日期、类型（跑步/力量训练/游泳/骑行/瑜伽/球类/HIIT/其他）、时长（分钟）、强度（低/中/高）、卡路里、体重 kg、备注
- **删除**：硬删除（无 `deleted_at`）
- **内置任务**：
  - `fitness_reminder` 每日 20:00 推送「该去健身啦」
  - `fitness_weekly_analysis` 每周日 19:00 汇总近 7 天记录，LLM 生成分析（失败降级为统计 Markdown）后走 `notify_for("report", …)`
- **实现**：`models/fitness.py` + `services/fitness_service.py` + `blueprints/fitness.py`
- **AI 工具**：`list_fitness_records` / `create_fitness_record` / `update_fitness_record` / `delete_fitness_record` / `analyze_fitness`

### 5.12 出行

- **页面** `/travel/`：出行计划列表 + 弹窗（目的地、往返日期、交通多选、多段交通、备选方案、预算、同行人、备注）
- **多段交通** `segments`：每段 `{type, from, to, depart_time, arrive_time, duration, platform, note}`（兼容旧字段 `time`）
- **备选方案** `alternatives`：`{title, desc}`
- **AI 生成行程**：`POST /travel/api/generate-itinerary` 调用 LLM，按天数写出逐日 Markdown，写入 `itinerary`
- **内置任务**：`trip_reminder` 每日 09:00 扫描「明天出发」的计划，有则推送汇总提醒
- **实现**：`models/travel.py` + `services/travel_service.py` + `blueprints/travel.py`
- **AI 工具**：`list_trip_plans` / `get_trip_plan` / `create_trip_plan` / `update_trip_plan` / `generate_trip_itinerary` / `delete_trip_plan`

### 5.13 消费 + App REST API

- **页面** `/expenses/`：记账列表；支持 `?month=YYYY-MM` 筛选；看板展示总支出/笔数/平均、分类占比、按月汇总
- **分类**：餐饮 / 交通 / 购物 / 娱乐 / 居住 / 医疗 / 教育 / 旅行 / 其他
- **支付方式**：微信 / 支付宝 / 现金 / 银行卡 / 其他
- **统计**：SQL 聚合（`SUM/COUNT/GROUP BY`），不拉全量
- **App API**（`blueprints/expenses_api.py`，前缀 `/api/v1/expenses`）：
  - 鉴权：请求头 `X-API-Token: <token>` 或 `Authorization: Bearer <token>`
  - **优先**匹配 `users.api_token`（「设置 → 账号」生成，数据记到该用户）
  - **回退** `.env` 的 `API_TOKEN`（记到第一个管理员，兼容旧配置）
  - 未配置任何 Token 时一律 401（防止无鉴权暴露）
  - 接口：`GET ""` 列表（`start_date`/`end_date`/`category`/`limit`）· `GET /stats` · `GET /<id>` · `POST ""` 创建 · `PUT|PATCH /<id>` · `DELETE /<id>`
- **实现**：`models/expense.py` + `services/expense_service.py` + `utils/api_auth.py`
- **AI 工具**：`list_expenses` / `get_expense_stats` / `create_expense` / `update_expense` / `delete_expense`

### 5.14 多用户

数据已按 `user_id` 隔离（见第 6 节），不再是「结构预留、实际单用户」。

- **账号**：`users.is_admin`；`users.feishu_open_id`（飞书发送者识别）
- **管理员**：设置页「用户」Tab 可创建用户（自动补一套内置定时任务）、绑定/解绑飞书 `open_id`（须 `ou_` 前缀）
- **飞书**：未绑定 `open_id` 的发送者会收到提示，请管理员去用户管理页绑定
- **设置**：`settings.user_id=0` 为全局（域名等）；`user_id>0` 为用户级（AI Key、渠道、简报等），读取时用户级优先
- **调度**：每个用户一套内置任务（`job_key` 相同，按 `user_id` 区分）；执行前 `scoping.set_current_user_id`
- **限制**：消费 App API 目前不按 Token 分用户；技能表仍全局（`name` unique）

### 5.15 后台安全入口（`ADMIN_ENTRY`）

`.env` 设置 `ADMIN_ENTRY=某路径` 后进入单域名隐藏后台模式：

- 后台必须通过 `https://域名/<入口>/…` 访问（如 `/abc123/login`）
- 不带入口访问后台路径一律 404；公开网页仍走 `/<slug>`
- 飞书回调 `/feishu/…` 免入口（自身有 token/加密）
- 改此项需**重启应用**（WSGI 中间件读取配置）

---

## 6. 数据库设计（17 张表）

> 约定：时间字段一律 **naive UTC** 存储，展示层按用户时区转换；日程/任务/笔记/网页/图片/技能/记忆为**软删除**（`deleted_at`）；健身/出行/消费为硬删除。个人数据表均有 `user_id`。

| 表 | 关键字段 | 说明 |
|---|---|---|
| `users` | username, password_hash, timezone, is_admin, feishu_open_id, api_token, prefs | 管理员可在设置页创建用户；飞书 `open_id` 用于消息归属；`api_token` 为 App REST 用户级鉴权；`prefs.theme` 为 light/dark/system |
| `events` | user_id, title, start_utc, end_utc, all_day, rrule, reminder_minutes, last_reminded_occurrence_utc, location, deleted_at | 重复事件按日期范围展开；记录已提醒的发生时间以持久化去重 |
| `tasks` | user_id, title, notes, due_utc, priority(1-3), status(open/done/cancelled), project, tags(JSON), completed_at, deleted_at | |
| `notes` | user_id, title, content, tags(JSON), deleted_at | AI 长期记忆素材 |
| `webpages` | user_id, title, slug(unique), description, content(HTML), is_public, enabled, deleted_at | 网页生成器；软删除时 slug 改写为 `<原>-d<id>` 释放 |
| `image_assets` | user_id, prompt, instruction, model, size, file_path, parent_id, is_public, deleted_at | 图片生成/改图记录；文件在 data/images/ |
| `skills` | name(unique), description, parameters(JSON Schema), code, enabled, deleted_at | AI 自写工具；新建默认禁用，执行走子进程（全局，无 user_id） |
| `memories` | user_id, content, source(auto/manual), importance(1-5), expires_at, deleted_at | 长期记忆；auto 每次梳理整体替换，过期自动遗忘 |
| `embeddings` | user_id, source_type, source_id, vector(JSON), model | RAG 语义检索向量索引 |
| `conversations` | user_id, title, summary, updated_at | 对话会话；summary 为上下文梳理写入的摘要 |
| `messages` | conversation_id(FK CASCADE), role(user/assistant/tool), content, tool_calls(JSON) | 对话消息 |
| `notifications` | user_id, channel, title, body, status(pending/sent/failed), error, read | 每渠道一条记录 |
| `scheduled_jobs` | user_id, job_key, name, action, cron, enabled, params(JSON), is_builtin, last_run_utc, last_status | 每用户一套内置任务；`job_key` 不再全局唯一 |
| `settings` | id, key, user_id, value(JSON) | `user_id=0` 全局，`>0` 用户级；唯一约束 `(key, user_id)` |
| `fitness_records` | user_id, date, workout_type, duration_min, intensity, calories, weight_kg, notes | 健身记录（硬删除） |
| `trip_plans` | user_id, destination, start_date, end_date, transports(JSON), segments(JSON), alternatives(JSON), budget, companions, notes, itinerary | 出行计划（硬删除） |
| `expense_records` | user_id, amount, category, date, payment_method, notes | 消费记录（硬删除） |

**运行时配置键（settings 表）**：`llm_base_url / llm_model / llm_api_key`、`sc_key`、`feishu_webhook_url / feishu_secret`、`feishu_app_id / feishu_app_secret / feishu_event_token / feishu_event_encrypt_key / feishu_app_target / feishu_app_target_type`、`default_channels`、`notify_briefing_channels / notify_report_channels / notify_backup_channels / notify_cleanup_channels / notify_reminder_channels`（场景渠道）、`briefing_time_morning / noon / evening`、`backup_enabled / backup_time / backup_keep / backup_channel`、`admin_domain / page_domain`、`image_base_url / image_api_key / image_model / image_size`、`feishu_chat_map`、`ai_persona_name / ai_persona_preset / ai_persona_verbosity / ai_persona_address / ai_persona_extra / ai_persona_ack_template / ai_persona_ack_enabled`（对话人设，用户级）。

---

## 7. 通知渠道架构（可插拔）

```
notify(title, body, channels=None, image_bytes=None)
        │
        ▼
┌──────────────────────────────────────┐
│ notify_service.notify()              │  ← 每渠道生成一条 Notification 记录
│  逐渠道：                            │     发送成功 → sent；失败 → failed + error
│  1. 渠道已注册？                     │
│  2. 渠道已配置（configured）？       │
│  3. ch.send(title, body, image_bytes)│
└──────────────────────────────────────┘
        │ 自动发现（pkgutil）
        ▼
app/services/channels/
├── __init__.py
├── serverchan.py   ServerChanChannel    （sctapi.ftqq.com SendKey → 微信）
├── feishu.py       FeishuChannel        （群机器人 Webhook + 可选加签）
└── feishu_app.py   FeishuAppChannel     （应用机器人 API，双向）
```

**新增渠道 = 两步**（示例见 `channels/feishu.py`）：

```python
@register_channel
class MyChannel(BaseChannel):
    name = "mychannel"                  # 唯一标识
    display_name = "我的渠道"            # 中文名
    @property
    def configured(self) -> bool:       # 读取 settings/.env
        return bool(get_setting_from("my_url", "MY_URL", ""))
    def send(self, title, body, image_bytes=None):  # 失败抛异常（记录到通知中心）；image_bytes 为可选图片字节
        requests.post(..., timeout=10)
```

模块放入 `channels/` 即被自动发现，设置页默认渠道选项、AI 工具、通知中心标签自动可见。

**图片支持**：`send` 的 `image_bytes` 为可选图片原始字节——飞书应用机器人（`feishu_app`）会 `im/v1/images` 上传后发 `msg_type:image`；飞书 Webhook（`feishu`）借用应用机器人上传拿 `image_key` 发图、失败退回文本；Server酱/站内仅文本（Server酱发图需公网 URL）。AI 工具 `send_notification` 新增可选 `image_id` 参数，可把图片库中已有图片随通知发送。

**场景渠道统一配置**（`notify_for(scene, title, body, explicit=None)`）：
- 三级路由：`explicit`（任务页覆盖，`params.channels`）> 场景设置（settings 键）> 全局默认渠道
- 场景与 settings 键（`notify_service.SCENE_KEYS`）：`briefing→notify_briefing_channels`、`report→notify_report_channels`、`backup→notify_backup_channels`（兼容旧 `backup_channel`）、`cleanup→notify_cleanup_channels`、`reminder→notify_reminder_channels`
- 设置页「通知渠道」tab 统一配置各场景渠道；简报/报告/备份/整理/提醒等动作统一走 `notify_for`

**自定义通知组**（多渠道联动同步推送）：
- 存 `settings` 表 `notify_groups` 键（`{"组名": ["inapp", "serverchan", ...]}`），设置页「通知渠道」tab 底部增删（`POST /settings/notify-group/add|delete`）
- 引用形式 `group:<组名>`：可用于默认渠道（多选）、各场景渠道、AI 工具 `send_notification`/`create_reminder_job` 的 channels、定时任务页渠道下拉
- 发送时 `notify()` 经 `expand_channels` 把组展开为组内全部渠道（去重保序、展开为空兜底站内）；删除组时自动清理默认/场景/定时任务中的引用
- `POST /settings/api/test-channel` 支持以组名发送测试通知

---

## 8. 定时任务系统

**架构**：`scheduled_jobs` 表是唯一事实来源 → 启动时 `SchedulerService.sync()` 同步到 APScheduler（启用任务 → `sj-{id}` 作业）→ 任务变更（增删改/开关）后 `reschedule()` 热重载。

**cron 格式**：
- `interval:N` —— 每 N 分钟
- 标准 5 段 cron（应用时区）：`分 时 日 月 周`，如 `0 7 * * *` = 每天 07:00

**内置任务（13 个，每用户一套）**：

| job_key | 说明 | 默认 |
|---|---|---|
| `event_reminder_scan` | 发送已到期且未发送的提醒，默认补发最近 60 分钟错过的扫描 | interval:1 |
| `task_due_scan` | 任务到期扫描（30 分钟内到期） | interval:10 |
| `morning_briefing` | 早安简报（对话式引导） | `0 7 * * *` |
| `noon_briefing` | 午间简报 | `0 12 * * *` |
| `evening_review` | 晚间复盘 | `0 21 * * *` |
| `data_backup` | 数据库备份（保留 N 份，默认 7） | `0 3 * * *` |
| `context_consolidation` | 上下文梳理（会话摘要 + 长期记忆抽取） | `0 4 * * *` |
| `weekly_report` | 周报（最近 7 天数据汇总，LLM 生成） | `0 18 * * 0` |
| `monthly_report` | 月报（最近 30 天数据汇总，LLM 生成） | `0 9 1 * *` |
| `weekly_cleanup` | 每周整理（归档超期任务/合并重复笔记/坏链体检/清理旧通知） | `0 2 * * 0` |
| `fitness_reminder` | 每日健身提醒 | `0 20 * * *` |
| `fitness_weekly_analysis` | 每周健身分析（近 7 天 + LLM） | `0 19 * * 0` |
| `trip_reminder` | 出行前一天提醒（扫描明天出发的计划） | `0 9 * * *` |

**动作注册**：任何模块 `@register_action("action_name")` 即可注册可调度动作（briefing / report / maintenance / backup / calendar / task / job / memory / fitness / travel）。自定义提醒动作 `custom_reminder` 由用户在前台创建（cron + 标题 + 内容 + 渠道）；**内置任务也可在任务页指定推送渠道**（保存到 `params.channels`，空=默认渠道）。动作签名为 `fn(user, params)`，调度线程通过 `scoping` 注入当前用户。

---

## 9. 路由 / API 清单

**页面路由**：`/login` `/logout` `/` `/setup`（安装向导，始终可打开、从第①步开始）`/calendar/` `/tasks/` `/notes/` `/pages/` `/pages/edit/<id>` `/images/` `/skills/` `/memory/` `/fitness/` `/travel/` `/expenses/` `/chat/` `/jobs/` `/notifications/` `/settings/` `/p/<slug>`（页面渲染；网页域名下 `/<slug>` 与 `/p/<slug>` 由 Host 路由处理，后台域名下私有页面需登录）`/img/<文件名>`（图片文件）

**JSON API**（`@csrf.exempt` + `@login_required`，返回 `{"ok": true, "data": ...}` / `{"ok": false, "error": "..."}`）：

| 方法 | 路由 | 说明 |
|---|---|---|
| POST | `/calendar/api/create · /update · /delete` | 事件 CRUD（JSON） |
| GET | `/calendar/api/events?year=&month=` | 月事件（含重复展开，用户时区字符串） |
| POST | `/tasks/api/create · /update · /toggle · /delete` | 任务 CRUD（JSON） |
| POST | `/notes/api/create · /update · /delete` | 笔记 CRUD（JSON） |
| POST | `/pages/api/save · /toggle · /duplicate · /delete` | 网页保存（新建/更新）/显示开关/复制/软删除（JSON） |
| GET | `/p/<slug>` | 页面渲染（后台域名/本地：私有页面需登录；网页域名：仅公开页面，隐藏/不存在 404） |
| POST | `/images/api/generate · /edit · /toggle-public · /delete` | 图片生成（同步，10-60s）/AI 改图/公开开关/软删除（JSON） |
| GET | `/img/<文件名>` | 图片文件（文件名白名单；公开任何人可访问，私有需登录，否则 404） |
| POST | `/skills/api/toggle · /delete` | 技能启用停用/软删除（JSON，启用即编译注册） |
| POST | `/memory/api/consolidate · /delete` | 手动上下文梳理/删除记忆（JSON） |
| GET | `/chat/api/conversations` | 会话列表 |
| GET | `/chat/api/messages/<id>` | 会话消息 |
| POST | `/chat/api/send` | **SSE 流式对话**（events: delta/ack/tool/title/done/error） |
| POST | `/chat/api/new · /delete/<id> · /clear-all` | 会话管理（一键清空所有会话，CASCADE 删消息） |
| POST | `/jobs/create · /update/<id> · /run/<id> · /delete/<id>` | 定时任务管理（表单） |
| POST | `/notifications/read/<id> · /mark-all-read · /clear-read · /delete/<id>` | 通知管理（表单） |
| POST | `/fitness/api/create · /update · /delete` | 健身记录 CRUD |
| POST | `/travel/api/create · /update · /delete · /generate-itinerary` | 出行计划 CRUD + AI 生成行程 |
| POST | `/expenses/api/create · /update · /delete` | 消费记录 CRUD（session 登录） |
| GET | `/api/v1/expenses` · `/stats` · `/<id>` | App 消费列表/统计/详情（用户 Token 或 `.env API_TOKEN`） |
| POST/PUT/PATCH/DELETE | `/api/v1/expenses` · `/<id>` | App 消费增改删（用户 Token 或 `.env API_TOKEN`） |
| POST | `/settings/api/api-token` | 生成/撤销当前用户 App Token（`{action: generate\|revoke}`） |
| POST | `/settings/api/theme` | 保存界面主题（`{theme: light\|dark\|system}`，写入 `users.prefs`） |
| POST | `/settings/account · /ai · /persona · /channels · /briefing · /feishu-app · /backup · /pages-domain · /image · /voice · /search · /vision` | 设置保存（表单；`/persona` 为人设） |
| POST | `/settings/users` · `/settings/users/bind` | 管理员创建用户 / 绑定飞书 open_id |
| POST | `/settings/api/test-channel · /test-llm · /test-feishu-app · /test-image · /backup-now` | 测试/立即执行（JSON） |
| GET/POST | `/settings/backups/download/<name> · /delete/<name>` | 备份文件下载/删除（路径穿越防护） |
| POST | `/feishu/event` | 飞书事件 HTTP 回调（**无登录、CSRF 豁免、token 握手**；SDK 模式下忽略消息） |
| GET | `/settings/api/feishu-ws-status` | 官方 SDK 长连接状态 |

**CLI**：`flask init-db`（建表+补列+管理员+每用户内置任务种子，幂等）· `flask create-admin` · `flask reset-db --yes`（DROP 当前库全部数据表，不加 `--yes` 只列出表名）· `flask reset-admin-password [--username 名] [--password 新密码]`（省略密码则随机生成并打印一次；多名管理员必须指定用户名）· `flask backup-now` · `flask restore-backup <path>` · `flask reindex`（重建 RAG 索引）· `flask routes`

---

## 10. 配置

### 10.1 .env（服务器级，启动读取）

| 变量 | 说明 |
|---|---|
| `SECRET_KEY` / `SESSION_COOKIE_SECURE` | 会话密钥 / HTTPS 时置 1 |
| `API_TOKEN` | 可选全局回退 Token（记到第一个管理员）。优先在「设置 → 账号」为每个用户生成独立 Token |
| `MYSQL_HOST/PORT/USER/PASSWORD/DB` | MySQL 连接 |
| `LLM_BASE_URL/LLM_API_KEY/LLM_MODEL/LLM_TIMEOUT` / `LLM_PROTOCOL` | 仅作首个管理员初始值。运行时 **每位用户自己的** 协议/地址/模型/Key（`openai` 或 `anthropic`），不共用 .env |
| `SC_KEY` / `FEISHU_WEBHOOK_URL` / `FEISHU_SECRET` | 渠道默认值（**可在网页覆盖**） |
| `FEISHU_APP_ID/SECRET/EVENT_TOKEN/EVENT_ENCRYPT_KEY` | 飞书应用机器人默认值（网页覆盖） |
| `FEISHU_RECEIVE_MODE` | 收消息接入：`callback`（HTTP 回调）或 `sdk`（官方长连接），网页可覆盖 |
| `DEFAULT_CHANNELS` | 默认推送渠道 |
| `ADMIN_DOMAIN` / `PAGE_DOMAIN` | 域名分离：后台域名 + AI 网页域名（网页可覆盖） |
| `ADMIN_ENTRY` | 后台安全入口路径（设置后须带此前缀访问后台，改后需重启） |
| `IMAGE_BASE_URL/IMAGE_API_KEY/IMAGE_MODEL/IMAGE_SIZE` | 图片生成服务默认值（OpenAI 兼容接口，网页可覆盖） |
| `VISION_BASE_URL/API_KEY/MODEL` | 视觉（多模态识图）模型默认值（OpenAI 兼容 chat/completions + 图片输入，网页可覆盖；未配置时飞书收图仅保存不识别） |
| `TTS_BASE_URL/API_KEY/MODEL/VOICE` | 语音朗读（OpenAI 兼容 `/audio/speech`；未配置时浏览器朗读） |
| `EMBEDDING_BASE_URL/API_KEY/MODEL` | RAG 语义检索嵌入服务（未配置降级关键词搜索） |
| `SEARCH_PROVIDER/SEARXNG_BASE_URL/SERPER_API_KEY/TAVILY_API_KEY` | 联网搜索提供方（代码默认 `bing` 免 key，网页可覆盖） |
| `ADMIN_USERNAME/ADMIN_PASSWORD` | init-db 首建管理员 |
| `APP_TIMEZONE` / `SCHEDULER_ENABLED` | 时区 / 调度总开关 |

### 10.2 配置优先级

**网页设置（settings 表）> .env > 代码默认值**。其中 **LLM API Key / 协议 / 地址 / 模型只读当前用户自己的记录**，不回退全局、不借用别人的 Key。通知渠道、简报、备份等仍是用户级优先于全局。AI 配置修改后**立即生效**。`SECRET_KEY` / `API_TOKEN` / `ADMIN_ENTRY` / 数据库连接改后需重启。

---

## 11. 部署

### 11.1 本地开发（Windows）

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env          # 填 MySQL / LLM Key
$env:FLASK_APP = "run:app"
.\.venv\Scripts\python -m flask init-db
.\.venv\Scripts\python run.py   # http://127.0.0.1:5000
```

### 11.2 生产部署（宝塔 / 普通 Linux / Docker）

完整步骤、MySQL 连库、网页端口、飞书、安全清单见 **[部署.md](部署.md)**。

要点：

- 生产入口 `wsgi.py`；waitress 单进程或多线程 gunicorn `-w 1`（APScheduler 不能多 worker）
- MySQL 预建 utf8mb4 库；`MYSQL_HOST` 在 Docker 中勿填 `127.0.0.1`
- 公开网页优先 `PAGE_PORT` 纯 HTTP；域名/Caddy 为可选
- `./data` 与 `.env` 必须持久化；升级后重复执行 `flask init-db`

---

## 12. 安全设计

| 项 | 措施 |
|---|---|
| 传输 | Caddy HTTPS + HSTS；应用仅监听容器内网 |
| 认证 | Werkzeug scrypt 密码哈希、session cookie（HttpOnly/SameSite）、登录限流 10 次/分 |
| CSRF | 全局 Flask-WTF；JSON API 豁免但要求登录 + SameSite=Lax |
| 输入 | 表单校验、软删除可回滚、备份文件下载路径穿越防护（resolve + 前缀校验） |
| 密钥 | SECRET_KEY / API Key 服务端持有，前端只见掩码（结尾 4 位） |
| 飞书回调 | URL 验证 token 握手、AES 解密、message_id 去重、CSRF 豁免但有 token 校验 |
| 网页生成器 | 私有页面未登录 404；隐藏页面（enabled=false）前台 404；slug 严格白名单校验 + 软删除释放；网页域名只服务页面渲染（无后台能力）；配置后台域名后非后台/网页域名（本地豁免）302 跳转后台域名；生成页面为作者自建内容，按预期渲染原始 HTML（等同自托管站点，勿生成包含凭据的内容） |
| 图片生成 | `/img/` 文件名严格白名单（uuid 格式）+ DB 记录校验，防路径穿越与枚举；私有图片未登录 404；图片 API Key 仅服务端持有；图片内容由外部模型生成，公开前请确认内容合规 |
| 联网抓取 | `urlsafety.validate_public_url` SSRF 防护：拒绝回环/私网/链路本地/云元数据 IP（IP 字面量与域名 DNS 解析双重校验），requests response hook 对重定向逐跳校验；应用于 fetch_page / 网页坏链体检 / 图片下载 |
| 自写技能 | 受限沙箱（内置白名单 + 静态扫描禁 import/子进程/网络/文件/反射/死循环）+ **子进程执行 + 10s 超时强杀** + 新建默认禁用 + 人工审核启用；**非强安全边界，依赖单用户信任，启用前务必审查代码** |
| 数据 | 每日自动备份（流式 + gzip，`.json.gz`，保留 N 份）+ 手动备份/一键导出下载；重置/恢复前强制自动备份、需当前密码 + 确认文字三重确认 |
| 限流 | Flask-Limiter 全局 300 次/分 + 登录专项限流 |
| App API | `/api/v1/*` 必须 Token（用户 `api_token` 或 `.env API_TOKEN`）；未配置一律 401 |
| 后台入口 | `ADMIN_ENTRY` 把后台藏到路径前缀下，未带前缀 404（飞书回调除外） |
| 多用户 | 个人数据按 `user_id` 过滤；用户管理仅 `is_admin` |

---

## 13. 测试体系（scripts/）

| 脚本 | 覆盖 | 结果 |
|---|---|---|
| `smoke_test.py` | 登录/8 页面/三模块 CRUD/通知/定时任务/简报/调度器自动提醒/SSE 对话 | 29/29 ✅ |
| `test_feishu_bot.py` | 飞书回调握手/接收/建会话/去重/加密/降级 | 12/12 ✅ |
| `test_backup_settings.py` | 备份配置/任务同步/立即备份/下载/删除/保留裁剪/渠道通知 | 17/17 ✅ |
| `test_ai_settings.py` | AI 配置网页修改/运行时生效/清除/校验 | 13/13 ✅ |
| `test_noon_briefing.py` | 午间简报设置/cron 同步/执行/通知 | 12/12 ✅ |
| `test_pages.py` | 网页生成器：创建/slug 校验/公开访问/隐藏/私有/编辑/复制/删除/域名分离（后台域名守卫 + 网页域名路由） | 34/34 ✅ |
| `test_pages_ai_e2e.py` | AI 端到端：对话 → create_page 工具真实生成网页 → 公开访问 → 清理（消耗一次 LLM 调用） | 4/4 ✅ |
| `test_images.py` | 图片：页面/路径安全/生成/访问控制/公开开关/AI 改图/删除（已配置图片服务时全测） | 21/21 ✅ |
| `test_images_ai_e2e.py` | AI 端到端：对话生成图片 + 对话改图（走 mock 图服务，消耗 LLM 调用） | 4/4 ✅ |
| `test_skills.py` | 技能：沙箱拒绝/默认禁用/启用注册执行/命名冲突/HTTP 启停删除 | 16/16 ✅ |
| `test_memory.py` | 记忆：长对话压缩/长期记忆抽取/上下文注入/手动梳理（消耗 LLM 调用） | 13/13 ✅ |
| `test_setup.py` | 安装引导：始终显示向导/从第①步开始/令牌校验/重置数据库/未初始化其它页跳 /setup | 38/38 ✅（活服务项可跳过） |
| `test_cli.py` | flask CLI：reset-db（无 --yes 不删 / --yes mock 删除）/ reset-admin-password（指定/随机/校验/非管理员） | 11/11 ✅ |
| `test_phase_a.py` | 阶段A：日程冲突检测/周报生成/主动早安引导/语音接口（消耗 LLM 调用） | 11/11 ✅ |
| `test_phase_b.py` | 阶段B：记忆重要度/过期衰减/每周整理（归档/合并/坏链/清通知） | 17/17 ✅ |
| `test_rag.py` | RAG：未配置降级/向量召回排序/索引挂钩（monkeypatch，无需真实嵌入服务） | 9/9 ✅ |
| `test_web_search.py` | 联网搜索：Bing/DDG/Serper/Tavily/SearXNG 解析、fetch_page、工具路径（mock） | 14/14 ✅ |
| `test_research_workflow.py` | 调研工作流端到端：真实搜索+LLM → 调研网页 → 知识库笔记（消耗 LLM 调用） | 7/7 ✅ |
| `test_common.py` | 测试公共工具：登录（429 限流自动退避），各脚本共用 | — |
| `test_notify_center.py` | 通知统一：场景渠道三级路由/explicit 覆盖/旧键兼容/设置页保存/备份动作 | 8/8 ✅ |
| `test_clear_chat.py` | 一键清空所有会话：创建/发送/清空/级联删消息 | 10/10 ✅ |
| `test_sandbox.py` | 技能模板/子进程执行/死循环超时强杀/ValueError 回传 | 7/7 ✅ |
| `mock_image_server.py` | 本地 mock OpenAI 兼容图片接口（b64/url 双模式 + /images/edits），无真实 Key 时验证全链路 | — |
| `test_grid_layout.py` / `test_wide_screen.py` | 布局元素与宽屏 CSS 规则 | 28/28 ✅ |
| `test_life_modules.py` | 健身/出行/消费 AI 工具 CRUD、重置表清单、App Token 按用户隔离 | 37/37 ✅ |
| `test_persona.py` | 对话人设：默认/自定义注入系统提示词、立即回复模板与开关、HTTP 保存、自定义必填、恢复默认 | 47/47 ✅ |
| `test_theme.py` | 界面主题：prefs 读写、非法值回退、API 保存、页面 data-theme-pref、顶栏/设置页/登录页切换 | 20/20 ✅ |
| `test_feishu_receive.py` | 飞书接入：parse_message、callback/sdk 切换、SDK 模式 HTTP 不重复处理、无公网 IPv4 无法打开构建网页的提示 | 24/24 ✅ |
| `test_page_port.py` | 网页独立 HTTP 端口：URL 为 http、公开页走端口、迷你站点渲染 | 10/10 ✅ |
| `test_db_session.py` | 失败事务回滚：`get_setting` / 设置页遇 PendingRollbackError 自动恢复 | 4/4 ✅ |
| `test_llm_protocol.py` | OpenAI/Anthropic 转换、每用户 Key、22 家官方 Base URL 快捷填入 | 61/61 ✅ |

---

## 14. 开发约定（新模块必读）

0. **改功能流程**：先阅读相关源码和测试，明确需求与影响范围 → 修改代码 → 运行相关测试 → 同步受影响的文档。本地环境和测试命令见 [开发指南](开发指南.md)。
1. **时间**：DB 一律 naive UTC（`timeutil.utcnow()`）；展示用 `user_tz(current_user)`；表单/接口时间字符串用 `parse_local(text, tz)` 解析
2. **软删除**：Event/Task/Note 删除置 `deleted_at`，查询默认过滤
3. **AI 工具**：`@register_tool(name, description, parameters, dangerous=...)`，返回 str/dict，非法参数抛 `ValueError`（错误回传模型自纠）
4. **调度动作**：`@register_action(name)`，签名 `fn(params: dict | None)`；调度线程无请求上下文，勿用 `current_user`
5. **JSON API**：`{"ok": true/false, "data"/"error"}`，失败 HTTP 400
6. **SSE 生成器**：不可直接使用路由内创建的 ORM 对象（上下文切换会脱离会话），先取纯 id 再在生成器内重新加载
7. **网页模块**：content 为完整 HTML 原样渲染（不转义）；slug 经 `page_service.validate_slug` 白名单校验；地址用 `page_service.page_public_url(page)`（优先独立 HTTP 端口，无需 HTTPS）；端口服务见 `page_site_server`
8. **图片模块**：文件只存 `data/images/`，文件名统一 `img-<uuid8>-<ts>.<ext>`；接口失败抛 `image_service.ImageError`（工具层转 ValueError 回传模型）；改图优先 /images/edits、失败降级重生成（parent_id 关联）
9. **技能模块**：code 为函数体、签名由 parameters.properties 推导；新建默认禁用；执行走 `skill_worker` 子进程（超时 `SKILL_TIMEOUT` 强杀）；受限沙箱是护栏非安全边界；启用前人工审查
10. **RAG 模块**：索引挂钩一律 try/except + `rag_service.is_configured()` 懒降级；向量归一化存库、检索用点积；未配置嵌入服务不影响主流程
11. **记忆模块**：会话摘要写 `conversations.summary`；长期记忆 source=auto 会被下次梳理整体替换，manual 保留；`build_messages` 注入顺序 = 系统提示词 → 长期记忆 → 会话摘要 → 最近消息
12. **用户隔离**：个人数据查询必须带 `user_id`（网页用 `current_user.id`，调度用 `scoping.current_user_id()`）；设置读写走 `settings_service`（用户级优先于全局）
13. **健身/出行/消费**：硬删除；日期 `YYYY-MM-DD`；非法参数抛 `ValueError`；调度动作签名 `fn(user, params)`；AI 工具在 `app/ai/tools/{fitness,travel,expense}_tools.py`
14. **App API**：`/api/v1/*` 一律 `@require_api_token`，未配置 Token 返回 401；不要用 session cookie 当 App 鉴权
15. **页面文案**：中文；模板复用 `base.html` + `style.css` 组件类；顶栏标题用 Bootstrap Icon，不要在 `page_title` 里再加 emoji
16. **侧栏入口**：加到 `nav_groups` 对应分组（工作/生活/智能/系统），不要只往扁平列表末尾塞

---

## 15. 已知限制与扩展方向

**已知限制**
- 消费 App API 的 `.env API_TOKEN` 仍是全局回退（记到第一个管理员）；请优先用「设置 → 账号」的用户 Token
- 技能表仍全局（`name` unique），未按用户隔离
- 重复事件不支持「单次例外」编辑（整体重复）
- 飞书消息支持文本与图片（其它富文本类型仍忽略）；识图需配置视觉模型
- SSE 单 worker 部署；多 worker 会让 APScheduler 重复跑
- API Key 等敏感配置明文存 settings 表（个人部署可接受）

**扩展方向**
- App API 去掉全局 `API_TOKEN` 回退，只保留用户 Token
- 📱 微信/Telegram 机器人桥接（复用工具注册表与对话执行器，参考飞书通道）
- 📡 iCal / CalDAV 同步手机日历
- 🔐 敏感配置加密存储（Fernet）
- 🌐 网页生成器：页面模板库、访问统计、版本历史
- 🎨 图片生成：多图批量、文生视频、图库标签检索

---

## 16. 变更记录

| 日期 | 内容 |
|---|---|
| 2026-08-24 | 修复设置页 `PendingRollbackError`：失败事务自动 rollback，定时任务异常先回滚再写状态，避免连接池把坏事务传给后续请求 |
| 2026-08-24 | 项目编辑流程 Skill `edit-flow`：先读源码、两轮确认、改代码、测试通过后再同步 PROJECT.md / README / 使用说明 |
| 2026-08-22 | 新增完整 [部署教程](部署.md)：宝塔 / 普通 Linux / Docker / MySQL 连库 / 网页端口 / 飞书 / 安全清单；compose 增加 mysql profile、.env 挂载、PAGE_PORT 映射 |
| 2026-08-23 | 对话模型按用户隔离 API Key；支持 OpenAI 兼容（chat.completions）与 Anthropic Messages（Claude）；设置页协议切换 + 快捷预设 |
| 2026-08-23 | 设置页「快捷填入官方地址」：国内/国际/本地共 22 家（DeepSeek/通义/Kimi/智谱/豆包/硅基流动/星火/混元/OpenAI/Claude/Grok/Gemini/Ollama 等） |
| 2026-08-17 | v0.1 初版：规划 → 骨架 → 六大模块（子模型并行）→ 集成验证 28/28 |
| 2026-08-17 | AI 模型配置网页化（保存即生效 + 测试连接） |
| 2026-08-17 | Grid 布局系统 + 宽屏/大屏适配 |
| 2026-08-17 | 午间简报（第三个定时简报）+ 修复简报 cron 时分写反 bug |
| 2026-08-17 | 飞书应用机器人（双向：接收 + AI 回复 + API 发送） |
| 2026-08-17 | 备份设置页（开关/时间/保留份数/渠道/下载/删除）+ 修复备份文件名同秒覆盖 + 修复 SSE 会话脱离 |
| 2026-08-17 | 网页生成器：AI 生成自由 HTML 网页 + 管理页（列表/在线编辑实时预览/复制/删除/显示开关）+ 公开访问（路径 /p/ 与子域名双模式）+ 6 个新 AI 工具（共 28 个） |
| 2026-08-18 | 图片生成：OpenAI 兼容 images 接口 + 图片库管理页 + AI 改图（/images/edits 编辑接口，不支持自动降级重生成）+ 公开/私有访问控制 + 对话内图片显示（mdLite 内联渲染）+ 6 个新 AI 工具（共 34 个） |
| 2026-08-18 | 域名分离：后台域名（admin.eugenstudio.cn）+ 网页域名（web.eugenstudio.cn，仅路径式 /<slug>）双域名体系，替换原子域名模式；非后台域名 302 守卫、网页域名仅服务页面渲染 |
| 2026-08-18 | 自进化能力：① AI 自写技能（受限沙箱 + 人工启用 + 技能管理页 + 6 工具）② 定时自动梳理上下文（会话摘要 + 长期记忆 + 内置任务 + 记忆页 + 3 工具）；AI 工具总数 43，数据表 13 张 |
| 2026-08-18 | 安装引导页（/setup）：仅系统未初始化时显示完整安装命令，已初始化自动跳转；登录页未初始化时显示引导链接 |
| 2026-08-18 | 阶段A：①日程冲突检测（create/update_event 自动查冲突 + ignore_conflicts）②主动早安（早报会话追加引导语）③周报/月报（report.py + 2 内置任务）④语音助手（🎤 浏览器 STT + 🔊 TTS 朗读，OpenAI 兼容 /audio/speech + 浏览器降级） |
| 2026-08-18 | 阶段B：①记忆重要度与衰减（importance/expires_at 列 + 抽取格式 + 自动遗忘）②每周自动整理（maintenance_service：归档超期任务/合并重复笔记/坏链体检/清旧通知 + 内置任务） |
| 2026-08-18 | 阶段C：①RAG 语义检索（embeddings 表 + rag_service + semantic_search 工具 + flask reindex + 索引挂钩）②技能模板生成器（get_skill_template）+ 沙箱升级（skill_worker 子进程执行 + 10s 超时强杀）；AI 工具总数 45，数据表 14 张 |
| 2026-08-18 | 联网搜索 + 调研工作流：web_search/fetch_page 工具（Bing 默认免 key 国内可用，可换 SearXNG/Serper/Tavily）；系统提示词规则：搜索→create_page 生成调研网页→回答；收录知识库→create_note 提炼核心（tag 调研）；mdLite 链接渲染；设置页「联网搜索」tab；AI 工具总数 47 |
| 2026-08-18 | 品牌更名 AiBot → 灵犀（Lingxi），全量替换源码/模板/文档/系统提示词（跳过备份数据与基础设施标识） |
| 2026-08-18 | UI 现代化：全新设计系统（设计令牌/毛玻璃顶栏/渐变卡片/微交互/响应式）+ Bootstrap Icons 自托管（本地 vendor，不依赖 CDN）+ 静态资源长期缓存消除导航图标闪烁 |
| 2026-08-18 | 备份升级：流式写出 + gzip（`.json.gz`）+ MySQL 服务端游标，内存占用恒定；兼容读取/删除历史 `.json` |
| 2026-08-18 | 数据管理：一键导出下载（`/settings/export`）、一键重置（`/settings/reset`）、恢复导入（`/settings/restore` + 上传）+ `flask restore-backup`；重置/恢复前自动安全备份 |
| 2026-08-18 | 飞书图片能力：① 机器人发图（AI 生成图片自动上传发回飞书）② 收图（下载存图库）③ 通知渠道支持 `image_bytes`（`send_notification` 新增 `image_id`） |
| 2026-08-18 | 视觉模型（多模态识图）：新增独立 VISION_* 配置 + vision_service + 设置页「视觉模型」tab；飞书收图时自动调用识别并回传描述（与沟通模型、图片生成模型三者独立） |
| 2026-08 | 多用户：业务表 `user_id`、管理员创建账号、飞书 `open_id` 绑定、每用户一套内置任务、设置分全局/用户级 |
| 2026-08 | 健身：训练记录 + 统计 + 每日提醒 + 每周 AI 分析（内置任务）；出行：多段交通/备选 + AI 生成行程 + 出发提醒；消费：记账看板 + `/api/v1/expenses`（`API_TOKEN`） |
| 2026-08 | `ADMIN_ENTRY` 后台安全入口；`API_TOKEN` App 鉴权 |
| 2026-08-22 | v1.2 文档对齐代码：表 17 张、内置任务 13 个、蓝图 22 个；补齐健身/出行/消费/多用户/App API |
| 2026-08-22 | 健身/出行/消费补齐 16 个 AI 工具（共 63）；一键重置纳入三张新表；App API 按 `users.api_token` 分用户（`.env API_TOKEN` 仍可回退管理员） |
| 2026-08-22 | 对话人设：设置页可改名字/性格预设/称呼/回复详细度/补充说明，写入系统提示词；工具规则优先于人设 |
| 2026-08-22 | 飞书收消息两种接入：HTTP 回调 / 官方 SDK（lark-oapi）长连接，后台可切换；消息处理抽到 feishu_inbound |
| 2026-08-22 | 后台界面：侧栏分组（工作/生活/智能/系统）+ AI 置顶；设置 Tab 五组吸顶；仪表盘快捷补健身/出行/消费/网页；顶栏标题改图标、去掉 emoji |
| 2026-08-22 | 网页站点：独立 HTTP 端口（PAGE_PORT），无需 HTTPS/域名；`page_site_server` 热启停 |
| 2026-08-23 | 界面主题：浅色 / 深色 / 跟随系统；顶栏与登录页切换，登录后写入 `users.prefs.theme` |
| 2026-08-23 | 对话立即回复：先确认再作答；模板可自定义（`{message}`/`{name}`/`{address}`），可关闭；网页流式首段、飞书先发一条 |
| 2026-08-24 | 安装向导第②步「重置数据库」：DROP 当前库全部数据表（不删库、不改 .env）；需安装令牌 + 二次确认；已初始化后禁用 |
| 2026-08-24 | 安装向导：库中已有数据表时不自动跳过第②步，须点「开始初始化」后才进入创建管理员 |
| 2026-08-24 | 安装向导严格按 ①→②→③→④ 点击前进：不检测管理员、已初始化也不跳离 /setup；写接口只校验安装令牌 |
| 2026-08-24 | flask CLI：`reset-db --yes` DROP 全部数据表；`reset-admin-password` 重设管理员密码（可随机打印一次） |
